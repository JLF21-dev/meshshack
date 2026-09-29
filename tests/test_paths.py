"""Telling radio from internet paths, including internet traffic that arrives without the MQTT flag."""

import pytest

from meshshack import paths
from meshshack.store import BROADCAST_NUM, Store

HOME = (40.1149296, -88.2280589)
NEAR, FAR, FLAGGED, OLD = 0x100, 0x200, 0x300, 0x400


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def node(store, num, dlat):
    store.record_node_info({"num": num, "user": {"shortName": f"N{num:x}"},
                            "position": {"latitude": HOME[0] + dlat, "longitude": HOME[1]}})


def pkt(store, pid, sender, hop_start=3, hop_limit=3, mqtt=False):
    p = {"from": sender, "to": BROADCAST_NUM, "id": pid, "rxSnr": -5.0, "decoded": {"portnum": "POSITION_APP"}}
    if hop_start is not None:
        p["hopStart"], p["hopLimit"] = hop_start, hop_limit
    if mqtt:
        p["viaMqtt"] = True
    store.record_packet(p)


def test_plausibility_limits():
    assert paths.max_plausible_km(0) == 100 and paths.max_plausible_km(4) == 250
    assert paths.packet_path(40, 0, False) == "direct" and paths.packet_path(40, 2, False) == "radio"
    assert paths.packet_path(397, 2, False) == "inferred"  # 397 km in 2 hops: not radio
    assert paths.packet_path(397, 2, True) == "mqtt" and paths.packet_path(None, 2, False) == "radio"
    assert paths.packet_path(40, None, False) == "unknown"


def test_every_node_is_marked(store):
    node(store, NEAR, 0.2)   # ~22 km
    node(store, FAR, 3.5)    # ~389 km
    node(store, FLAGGED, 3.5)
    pkt(store, 1, NEAR)                           # direct
    pkt(store, 2, FAR, hop_start=3, hop_limit=1)  # 389 km in 2 hops: probably internet, unflagged
    pkt(store, 3, FLAGGED, mqtt=True)
    pkt(store, 4, OLD, hop_start=None)            # no hop information
    pkt(store, 5, NEAR, hop_start=3, hop_limit=2)
    s = paths.summarize(store, (*HOME, None))
    assert s[NEAR]["kind"] == "direct" and s[NEAR]["direct"] == 1 and s[NEAR]["radio"] == 1
    assert s[FAR]["kind"] == "inferred" and s[FAR]["label"] == "Internet?"
    assert "389 km away in 2 hops" in s[FAR]["why"] and "without the MQTT flag" in s[FAR]["why"]
    assert s[FLAGGED]["kind"] == "mqtt" and s[OLD]["kind"] == "unknown"
    pkt(store, 6, FAR, hop_start=7, hop_limit=0)  # 7 hops could cover it: now also over radio
    assert paths.summarize(store, (*HOME, None))[FAR]["kind"] == "both"
    # Without a station position nothing can be judged by distance, so it stays radio.
    assert paths.summarize(store, (None, None, None))[FAR]["kind"] == "radio"
