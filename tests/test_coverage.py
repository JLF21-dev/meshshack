"""Coverage analysis: geometry, relay matching, and the report, from logged packets only."""

import pytest

from meshshack.coverage import (
    bearing_deg, compass, distance_range_km, relay_candidates, report, snr_limit, station_from_store,
)
from meshshack.store import BROADCAST_NUM, Store

HOME = (40.1149296, -88.2280589)  # the UIUC ECE Building
NEAR, FAR, TRACKER = 0x0A0B0C90, 0x0D0E0FC0, 0x11223390  # NEAR and TRACKER both end in 0x90


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def packet(store, pid, sender, snr, hop_start=3, hop_limit=3, relay=None, mqtt=False):
    p = {"from": sender, "to": BROADCAST_NUM, "id": pid, "rxSnr": snr, "rxRssi": -100, "hopStart": hop_start,
         "decoded": {"portnum": "POSITION_APP"}}
    if hop_limit:  # 0 is left out, as the protobuf-to-dict conversion does
        p["hopLimit"] = hop_limit
    if relay is not None:
        p["relayNode"] = relay
    if mqtt:
        p["viaMqtt"] = True
    store.record_packet(p)


def test_geometry():
    assert compass(bearing_deg(*HOME, HOME[0] + 0.1, HOME[1])) == "N"
    assert compass(bearing_deg(*HOME, HOME[0], HOME[1] + 0.1)) == "E"
    assert compass(bearing_deg(*HOME, HOME[0] - 0.1, HOME[1] - 0.1)) == "SW"
    exact = distance_range_km(*HOME, HOME[0] + 0.1, HOME[1], None)
    assert exact[0] == exact[1] == pytest.approx(11.12, abs=0.05)
    # A 13-bit rounded position is somewhere in a ~5.8 km tall cell: a range, containing the centre.
    center = (40.1342464, -88.211456)
    near, far = distance_range_km(*HOME, *center, 13)
    assert near == 0  # home is inside that very cell
    assert far > distance_range_km(*HOME, *center, None)[0]
    assert snr_limit("LONG_FAST") == -17.5 and snr_limit("SHORT_FAST") == -7.5 and snr_limit(None) is None


def test_relay_candidates_prefer_direct_neighbors():
    assert relay_candidates(0x90, {NEAR}, {NEAR, TRACKER}) == [NEAR]
    assert relay_candidates(0x90, set(), {NEAR, TRACKER}) == [NEAR, TRACKER]  # ambiguous
    assert relay_candidates(0x42, {NEAR}, {NEAR}) == []


def test_report(store):
    store.record_node_info({"num": NEAR, "user": {"shortName": "NEAR"}, "position": {"latitude": HOME[0] + 0.05,
                                                                                      "longitude": HOME[1]}})
    store.record_node_info({"num": TRACKER, "user": {"shortName": "TRK"}})
    for i, snr in enumerate([-4.0, -6.0, -9.0]):
        packet(store, i, NEAR, snr)  # direct
    for i in range(6):
        packet(store, 100 + i, 0x5555, -12.0, hop_start=3, hop_limit=1, relay=0x90)  # via NEAR
    packet(store, 200, 0x6666, -15.0, hop_start=7, hop_limit=0, relay=0xC0)  # used every hop; relay unknown
    packet(store, 300, 0x7777, 5.0, mqtt=True)
    store.record_packet({"from": 0x8888, "to": BROADCAST_NUM, "id": 400, "rxSnr": 1.0,  # old firmware
                         "decoded": {"portnum": "POSITION_APP"}})

    rep = report(store, (*HOME, None), "LONG_FAST")
    [n] = rep["neighbors"]
    assert (n["short_name"], n["packets"], n["snr_median"], n["snr_best"], n["snr_worst"]) == ("NEAR", 3, -6.0, -4.0, -9.0)
    assert n["margin_db"] == pytest.approx(11.5) and n["bearing"] == "N"
    assert n["distance_km"] == pytest.approx(5.56, abs=0.05) and not n["rounded"]

    by_kind = {(s["kind"], s.get("byte")): s for s in rep["sources"]}
    assert by_kind[("relay", 0x90)]["candidates"] == [NEAR] and by_kind[("relay", 0x90)]["certain"]
    assert by_kind[("relay", 0x90)]["label"] == "Via NEAR (ID ends 0x90)"
    assert by_kind[("relay", 0xC0)]["label"] == "Via an unknown relay (ID ends 0xc0)"
    assert rep["totals"] == {"heard": 12, "direct": 3, "relayed": 7, "mqtt": 1, "unknown_path": 1}
    assert sum(s["share"] for s in rep["sources"]) == pytest.approx(1.0)  # every packet is accounted for

    # No station position: distances are unknown, the rest still works.
    [n] = report(store, (None, None, None), None)["neighbors"]
    assert n["distance_range_km"] is None and n["margin_db"] is None


def test_station_from_store(store):
    assert station_from_store(store) == (None, None, None)
    store.set_station("fixed_position", {"latitude": HOME[0], "longitude": HOME[1], "altitude": 0})
    assert station_from_store(store) == (*HOME, None)


def test_coverage_tab(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    store.record_node_info({"num": NEAR, "user": {"shortName": "NEAR"}, "position": {"latitude": HOME[0] + 0.05,
                                                                                      "longitude": HOME[1]}})
    store.set_station("fixed_position", {"latitude": HOME[0], "longitude": HOME[1], "altitude": 0})
    packet(store, 1, NEAR, -6.0)
    packet(store, 2, 0x5555, -12.0, hop_start=3, hop_limit=1, relay=0x90)
    win = MainWindow(tmp_path / "test.db")
    try:
        win.show()
        win.tabs.setCurrentWidget(win.coverage)
        app.processEvents()
        tab = win.coverage
        assert tab.neighbors.rowCount() == 1 and tab.neighbors.item(0, 2).text() == "N"
        assert tab.sources.item(1, 0).text() == "Via NEAR (ID ends 0x90)"
        assert "2</b> packets" in tab.summary.text() or "Heard <b>2</b>" in tab.summary.text()
    finally:
        win.close()


def test_coverage_over_time(store):
    import time as _time
    day = 86400
    start = _time.mktime((2026, 9, 20, 0, 0, 0, 0, 0, -1))
    packet(store, 1, NEAR, -6.0)  # direct
    store._conn.execute("UPDATE packets SET logged_at = ?", (start + 3600,))
    store._conn.commit()
    for i in range(3):
        store.record_packet({"from": 0x5555, "to": BROADCAST_NUM, "id": 10 + i, "rxSnr": -9.0, "hopStart": 3,
                             "hopLimit": 1, "relayNode": 0x90, "decoded": {"portnum": "POSITION_APP"}}, now=start + day + i)
    rows = store.coverage_over_time(start, day)
    assert [(r["heard"], r["direct"], r["neighbors"], r["relays"]) for r in rows] == [(1, 1, 1, {}), (3, 0, 0, {0x90: 3})]
    assert rows[1]["start"] == start + day


def test_over_time_view_and_notes(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow
    from meshshack.gui.coverage import OverTimeChart

    app = QApplication.instance() or QApplication([])
    packet(store, 1, NEAR, -6.0)
    packet(store, 2, 0x5555, -12.0, hop_start=3, hop_limit=1, relay=0x90)
    store.set_station("coverage_notes", [{"at": __import__("time").time() - 60, "text": "antenna up"}])
    win = MainWindow(tmp_path / "test.db")
    try:
        win.show()
        win.tabs.setCurrentWidget(win.coverage)
        win.coverage.views.setCurrentWidget(win.coverage.over_time)
        app.processEvents()
        charts = win.coverage.over_time.findChildren(OverTimeChart)
        titles = sorted(c.chart().title() for c in charts)
        assert len(charts) == 3 and titles[0].startswith("Heard directly")
        relay_chart = next(c for c in charts if "relay" in c.chart().title())
        names = [s.name() for s in relay_chart.chart().series() if s.name()]
        assert names == ["via !0a0b0c90"]  # the relay, identified (it has no short name in this test)
    finally:
        win.close()
