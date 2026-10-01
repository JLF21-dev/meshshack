"""MeshCore logging: packets, adverts, contacts and messages from a second radio (receive only)."""

import asyncio
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from meshshack import devices
from meshshack.meshcore_collector import MeshCoreCollector
from meshshack.paths import mc_via
from meshshack.store import Store

HOME = (40.1149296, -88.2280589)  # the UIUC ECE Building
KEY = "a0" * 32
REPEATER = "b1" * 32


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def advert_packet(key=KEY, name="TESTNODE", adv_type=1, lat=None, lon=None, timestamp=1_790_000_000,
                  route=1, path=b""):
    """A raw MeshCore advert as it goes over the air: header (route, payload type 4), path length and
    hashes, then public key, timestamp, signature, flags, optional position, name."""
    flags = adv_type | 0x80 | (0x10 if lat is not None else 0)
    body = bytes.fromhex(key) + timestamp.to_bytes(4, "little") + bytes(64) + bytes([flags])
    if lat is not None:
        body += int(lat * 1e6).to_bytes(4, "little", signed=True) + int(lon * 1e6).to_bytes(4, "little", signed=True)
    body += name.encode()
    return bytes([(4 << 2) | route, len(path)]) + path + body


def parsed(raw, snr=-5.5, rssi=-98):
    """What the collector gets: the meshcore library's own parse of a received packet."""
    parser = pytest.importorskip("meshcore.meshcore_parser").MeshcorePacketParser()
    return asyncio.run(parser.parsePacketPayload(raw, {"snr": snr, "rssi": rssi, "payload": raw.hex()}))


def test_hops():
    assert Store.mc_hops("FLOOD", 3) == 3 and Store.mc_hops("TC_FLOOD", 0) == 0
    assert Store.mc_hops("DIRECT", 0) == 0  # sent straight to us
    assert Store.mc_hops("DIRECT", 2) is None  # a set route: the path is what's still ahead
    assert Store.mc_hops("FLOOD", None) is None


def test_adverts_make_nodes(store):
    lat, lon = HOME[0] + 0.05, HOME[1]
    store.mc_record_packet(parsed(advert_packet(lat=lat, lon=lon, path=b"\x11\x22"), snr=-9.0), now=1000)
    [n] = store.mc_nodes()
    assert (n["name"], n["type"], n["min_hops"], n["adverts"]) == ("TESTNODE", 1, 2, 1)
    assert n["latitude"] == pytest.approx(lat) and n["last_snr"] is None  # relayed: no SNR for the node itself
    # The same advert straight from it: fewer hops, and now a signal reading; still one advert.
    store.mc_record_packet(parsed(advert_packet(lat=lat, lon=lon), snr=6.25, rssi=-70), now=1001)
    [n] = store.mc_nodes()
    assert (n["min_hops"], n["last_hops"], n["last_snr"], n["last_rssi"], n["adverts"]) == (0, 0, 6.25, -70, 1)
    assert n["direct_heard"] == 1001
    # An older advert replayed later doesn't move it back.
    store.mc_record_packet(parsed(advert_packet(lat=10.0, lon=10.0, timestamp=1_780_000_000)), now=1002)
    [n] = store.mc_nodes()
    assert n["latitude"] == pytest.approx(lat)
    [p] = store.mc_packets(limit=1)
    assert (p["payload_type"], p["route"], p["public_key"]) == ("ADVERT", "FLOOD", KEY)
    counts, by_type = store.mc_stats()
    assert counts["packets"] == 3 and by_type[0]["direct"] == 2


def test_contacts_and_messages(store):
    store.mc_record_contact({"public_key": REPEATER, "type": 2, "adv_name": "Tower", "last_advert": 900,
                             "adv_lat": 0.0, "adv_lon": 0.0}, now=1000)
    [n] = store.mc_nodes()
    assert (n["name"], n["type"], n["is_contact"], n["latitude"], n["last_heard"]) == ("Tower", 2, 1, None, 900)
    assert n["heard_by_us"] is None  # only listed by the radio
    store.mc_record_contact({"public_key": KEY, "type": 1, "adv_name": "Clock", "last_advert": 99_999}, now=1000)
    assert store.mc_node_by_prefix(KEY[:12])["last_heard"] is None  # a future timestamp isn't "heard"

    store.mc_record_message({"type": "CHAN", "channel_idx": 0, "path_len": 2, "sender_timestamp": 5,
                             "text": "Tower: hello: world", "SNR": -3.5}, channel_name="Public")
    store.mc_record_message({"type": "PRIV", "pubkey_prefix": KEY[:12], "path_len": 255, "text": "hi"})
    dm, chan = store.mc_messages()
    assert (chan["channel_name"], chan["sender"], chan["text"], chan["hops"], chan["snr"]) == \
        ("Public", "Tower", "hello: world", 2, -3.5)
    assert (dm["channel"], dm["pubkey_prefix"], dm["hops"], dm["text"]) == (None, KEY[:12], None, "hi")
    assert [m["text"] for m in store.mc_messages(channel=0)] == ["hello: world"]


def test_via(store):
    station = (*HOME, None)
    store.mc_record_packet(parsed(advert_packet(lat=HOME[0] + 0.05, lon=HOME[1])), now=1000)
    store.mc_record_packet(parsed(advert_packet(key=REPEATER, name="Far", lat=HOME[0] + 5, lon=HOME[1],
                                                path=b"\x01")), now=1000)
    store.mc_record_contact({"public_key": "c2" * 32, "type": 1, "adv_name": "Listed", "last_advert": 500}, now=1000)
    by_name = {n["name"]: mc_via(n, station) for n in store.mc_nodes()}
    assert by_name["TESTNODE"][0] == "direct"
    assert by_name["Far"][0] == "inferred" and "556 km away in 1 hop" in by_name["Far"][2]
    assert by_name["Listed"][0] == "listed"
    assert mc_via(store.mc_node_by_prefix("a0a0"), (None, None, None))[0] == "direct"  # no station position


def test_collector_events_and_linking(store, monkeypatch):
    pytest.importorskip("meshcore")
    c = MeshCoreCollector(store)
    monkeypatch.setattr(devices, "scan", lambda list_ports=None: [])
    assert c._find_port() is None  # nothing linked: it never touches a radio
    devices.link(store, "240AC4000002", "meshcore")
    assert c._find_port() is None  # linked but not plugged in
    assert c.status() == {"connected": False, "linked": "240AC4000002"}

    c.channels = {0: "Public"}
    c._on_packet(SimpleNamespace(payload=parsed(advert_packet(name="Heard"))))
    c._on_message(SimpleNamespace(payload={"type": "CHAN", "channel_idx": 0, "path_len": 0, "text": "Heard: hi"}))
    assert store.mc_nodes()[0]["name"] == "Heard"
    assert store.mc_messages()[0]["channel_name"] == "Public"


def test_api(store, tmp_path):
    from meshshack.api import ApiServer, Radio

    store.mc_record_packet(parsed(advert_packet()), now=1000)
    store.mc_record_message({"type": "CHAN", "channel_idx": 0, "path_len": 1, "text": "A: b"}, channel_name="Public")
    server = ApiServer(Radio(SimpleNamespace(iface=None, connected_port=None), store), tmp_path / "hub.json", port=0)
    server.start()
    reader = store.create_token("reader", {"read"})

    def get(path):
        req = urllib.request.Request(server.url + path, headers={"Authorization": f"Bearer {reader}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    try:
        assert get("/api/meshcore/status") == {"connected": False, "linked": None}
        [n] = get("/api/meshcore/nodes")["nodes"]
        assert (n["name"], n["type_name"], n["via"]) == ("TESTNODE", "chat", "direct")
        assert get("/api/meshcore/messages")["messages"][0]["sender"] == "A"
        assert get("/api/meshcore/packets")["packets"][0]["payload_type"] == "ADVERT"
    finally:
        server.stop()


def test_gui_shows_meshcore_nodes(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow
    from meshshack.gui.nodes import COLUMNS

    app = QApplication.instance() or QApplication([])
    store.record_node_info({"num": 0x0A0B0C0D, "user": {"id": "!0a0b0c0d", "shortName": "MT1"},
                            "position": {"latitude": HOME[0] + 0.02, "longitude": HOME[1]}})
    store.record_packet({"from": 0x0A0B0C0D, "to": 0xFFFFFFFF, "id": 1, "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}})  # heard, so it's listed
    store.mc_record_packet(parsed(advert_packet(name="Tower", adv_type=2, lat=HOME[0] + 0.05, lon=HOME[1])))
    win = MainWindow(tmp_path / "test.db")
    try:
        nodes = win.nodes
        nodes.network_box.setCurrentIndex(nodes.network_box.findData("both"))
        nodes.refresh()
        networks = sorted(nodes.table.item(r, COLUMNS.index("Network")).text() for r in range(nodes.table.rowCount()))
        assert networks == ["MeshCore", "Meshtastic"]
        row = next(r for r in range(nodes.table.rowCount()) if nodes.table.item(r, 0).text() == "Tower")
        assert nodes.table.item(row, COLUMNS.index("Hardware")).text() == "repeater"
        assert nodes.table.item(row, COLUMNS.index("Via")).text() == "Direct"
        nodes.table.selectRow(row)
        app.processEvents()
        assert nodes._selected_num() is None and not nodes.buttons["dm"].isEnabled()  # no MeshCore actions yet
        assert "MeshCore repeater" in nodes.details.toPlainText()
        nodes.network_box.setCurrentIndex(nodes.network_box.findData("meshtastic"))
        assert nodes.table.rowCount() == 1

        map_tab = win.map
        for network, expected in (("both", {"meshtastic", "meshcore"}), ("meshcore", {"meshcore"}),
                                  ("meshtastic", {"meshtastic"})):
            map_tab.network_box.setCurrentIndex(map_tab.network_box.findData(network))
            payload = map_tab._payload()
            assert {n["net"] for n in payload["nodes"]} == expected, network
        map_tab.network_box.setCurrentIndex(map_tab.network_box.findData("meshcore"))
        [mc] = map_tab._payload()["nodes"]
        assert (mc["key"], mc["type"], mc["path"]) == ("mc:" + KEY, "repeater", "direct")
        assert map_tab.count.text() == "1 MeshCore with a position"
    finally:
        win.close()


def test_chat_keeps_networks_apart(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    store.record_packet({"from": 0x0A0B0C0D, "to": 0xFFFFFFFF, "id": 7, "channel": 0, "rxSnr": 4.0, "hopStart": 3,
                         "hopLimit": 3, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "on meshtastic"}})
    store.set_station("meshcore_radio", {"name": "TESTNODE", "channels": {"0": "Public"}})
    row = store.mc_record_message({"type": "CHAN", "channel_idx": 0, "path_len": 0, "SNR": 7.5,
                                   "text": "Tower: on meshcore, SOS"}, channel_name="Public")
    from meshshack import alerts
    alert = alerts.check_meshcore(store, row, "on meshcore, SOS", sender="Tower", channel=0, channel_name="Public")
    assert alert["reason"] == "Keyword: SOS (MeshCore #Public)" and alert["loud"]
    [a] = store.alerts()
    assert (a["network"], a["from_short"], a["hops"], a["rx_snr"]) == ("meshcore", "Tower", 0, 7.5)

    win = MainWindow(tmp_path / "test.db")
    try:
        chat = win.chat
        chat.refresh()
        titles = [chat.conv_list.item(i).text().strip() for i in range(chat.conv_list.count())]
        assert titles[0] == "MESHTASTIC" and "MESHCORE" in titles
        mc_at = titles.index("MESHCORE")
        assert any(t.startswith("# Public") for t in titles[mc_at:])  # MeshCore's Public, in its own section
        assert not any(t.startswith("# Public") for t in titles[:mc_at])

        chat.current = ("mc_channel", 0)
        chat.refresh()
        text = chat.view.toPlainText()
        assert "Tower" in text and "on meshcore" in text and "on meshtastic" not in text
        assert "direct · SNR 7.5 dB" in text and "Keyword: SOS" in text
        assert "MeshCore" in chat.header.text() and not chat.input.isEnabled()

        chat.current = ("channel", 0)
        chat.refresh()
        assert "on meshtastic" in chat.view.toPlainText() and "on meshcore" not in chat.view.toPlainText()
        assert "Meshtastic" in chat.header.text() and chat.input.isEnabled()

        win.alert_center.current = store.alerts()[0]
        win.alert_center._open()  # "Open" on a MeshCore alert goes to the MeshCore conversation
        assert chat.current == ("mc_channel", 0) and win.tabs.currentWidget() is chat
    finally:
        win.close()
