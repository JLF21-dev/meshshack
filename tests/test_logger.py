"""Tests run real protobuf packets through the meshtastic library's own decoding and pubsub
path, so the dict shapes the logger sees match what a radio produces."""

import json
import threading
import time

import meshtastic.mesh_interface
import meshtastic.serial_interface
import meshtastic.util
import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

from meshshack import cli
from meshshack.collector import Collector
from meshshack.store import BROADCAST_NUM, Store, to_jsonable

ALICE = 0xA1B2C3D4
BOB = 0x0BADBEEF


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def radio(store):
    """A MeshInterface with no device behind it, plus a Collector listening to it."""
    collector = Collector(store)
    from pubsub import pub

    pub.subscribe(collector.on_receive, "meshtastic.receive")
    pub.subscribe(collector.on_node_updated, "meshtastic.node.updated")
    iface = meshtastic.mesh_interface.MeshInterface(noProto=True)
    iface.nodes, iface.nodesByNum = {}, {}  # normally set up during the config download
    yield iface, collector
    pub.unsubscribe(collector.on_receive, "meshtastic.receive")
    pub.unsubscribe(collector.on_node_updated, "meshtastic.node.updated")
    iface.close()


def make_packet(sender, portnum=None, payload=b"", to=BROADCAST_NUM, **fields):
    pkt = mesh_pb2.MeshPacket(to=to, id=fields.pop("id", 1234), rx_snr=6.25, rx_rssi=-92,
                              hop_limit=2, hop_start=3, channel=0, **fields)
    setattr(pkt, "from", sender)
    if portnum is not None:
        pkt.decoded.portnum = portnum
        pkt.decoded.payload = payload
    return pkt


def deliver(iface, store, pkt, expect_packets):
    iface._handlePacketFromRadio(pkt)
    # The library publishes on a background thread; wait for the write to land.
    deadline = time.time() + 5
    while time.time() < deadline:
        if store.stats()[0]["packets"] >= expect_packets:
            return
        time.sleep(0.02)
    raise AssertionError("packet was not recorded")


def test_text_message(radio, store):
    iface, _ = radio
    deliver(iface, store, make_packet(ALICE, portnums_pb2.TEXT_MESSAGE_APP, b"hello mesh"), 1)

    [msg] = store.messages()
    assert msg["text"] == "hello mesh"
    assert msg["from_id"] == "!a1b2c3d4"
    assert msg["is_direct"] == 0
    assert msg["rx_snr"] == 6.25 and msg["rx_rssi"] == -92

    # One hop: the signal belongs to the relay, so the node gets hops but no SNR/RSSI of its own.
    [node] = store.nodes()
    assert node["num"] == ALICE and node["hops_away"] == 1 and node["last_rssi"] is None
    assert node["rf_heard"] is not None and node["direct_heard"] is None


def test_direct_message(radio, store):
    iface, _ = radio
    deliver(iface, store, make_packet(ALICE, portnums_pb2.TEXT_MESSAGE_APP, b"psst", to=BOB), 1)
    assert store.messages()[0]["is_direct"] == 1


def test_nodeinfo_then_position_then_telemetry(radio, store):
    iface, _ = radio
    user = mesh_pb2.User(id="!a1b2c3d4", long_name="Alice Base", short_name="ALB",
                         hw_model=mesh_pb2.HardwareModel.HELTEC_V3, role=2)
    deliver(iface, store, make_packet(ALICE, portnums_pb2.NODEINFO_APP, user.SerializeToString()), 1)

    pos = mesh_pb2.Position(latitude_i=401149000, longitude_i=-882281000, altitude=160, sats_in_view=9)
    deliver(iface, store, make_packet(ALICE, portnums_pb2.POSITION_APP, pos.SerializeToString(), id=2), 2)

    tel = telemetry_pb2.Telemetry(time=1700000000, device_metrics=telemetry_pb2.DeviceMetrics(
        battery_level=87, voltage=4.01, channel_utilization=12.5))
    deliver(iface, store, make_packet(ALICE, portnums_pb2.TELEMETRY_APP, tel.SerializeToString(), id=3), 3)

    [node] = store.nodes()
    assert (node["long_name"], node["short_name"], node["hw_model"]) == ("Alice Base", "ALB", "HELTEC_V3")
    assert node["role"] == "ROUTER"
    assert node["latitude"] == pytest.approx(40.1149) and node["longitude"] == pytest.approx(-88.2281)
    assert node["battery_level"] == 87 and node["voltage"] == pytest.approx(4.01)

    [position] = store._query("SELECT * FROM positions")
    assert position["sats_in_view"] == 9 and position["altitude"] == 160

    [telemetry] = store.telemetry()
    assert telemetry["kind"] == "deviceMetrics"
    assert json.loads(telemetry["metrics"])["channelUtilization"] == 12.5

    assert store.node_label(ALICE) == "!a1b2c3d4 (ALB)"


def test_encrypted_packet_is_still_logged(radio, store):
    iface, _ = radio
    deliver(iface, store, make_packet(BOB, encrypted=b"\x01\x02\x03"), 1)
    [pkt] = store.packets()
    assert pkt["portnum"] == "ENCRYPTED"
    assert json.loads(pkt["json"])["encrypted"]  # stored, and JSON-serializable
    assert store.nodes()[0]["num"] == BOB  # we still learn the node exists


def test_node_database_snapshot(store):
    store.record_node_info({
        "num": BOB,
        "user": {"id": "!0badbeef", "longName": "Bob Mobile", "shortName": "BOB", "hwModel": "TBEAM"},
        "position": {"latitude": 40.1100, "longitude": -88.2800},
        "lastHeard": 1700000000,
        "snr": 3.5,
        "hopsAway": 2,
    })
    [node] = store.nodes()
    assert node["role"] == "CLIENT" and node["hops_away"] == 2 and node["last_heard"] == 1700000000
    assert node["last_snr"] is None  # the radio's snr may be a relay's; only our direct packets set it

    # An older lastHeard from the radio must not move last_heard backwards.
    store.record_node_info({"num": BOB, "lastHeard": 1600000000})
    assert store.nodes()[0]["last_heard"] == 1700000000
    assert store.nodes()[0]["long_name"] == "Bob Mobile"  # missing fields don't erase known ones


def test_heard_via_counts_radio_and_mqtt_packets(store):
    def pkt(sender, pid, mqtt):
        store.record_packet({"from": sender, "to": BROADCAST_NUM, "id": pid, "viaMqtt": mqtt,
                             "decoded": {"portnum": "POSITION_APP"}})
    pkt(ALICE, 1, False)
    pkt(ALICE, 2, False)
    pkt(BOB, 3, True)
    pkt(BOB, 4, False)
    assert store.heard_via() == {ALICE: (2, 0), BOB: (1, 1)}


def test_to_jsonable_strips_protobufs_and_bytes():
    out = to_jsonable({"raw": object(), "decoded": {"payload": b"\xff\x00", "raw": object()}, "x": [1, b"a"]})
    assert out == {"decoded": {"payload": "ff00"}, "x": [1, "61"]}


def test_describe_line(radio, store):
    _, collector = radio
    line = collector.describe({"from": ALICE, "to": BROADCAST_NUM, "rxSnr": 5.0, "rxRssi": -100,
                               "hopStart": 3, "hopLimit": 3,
                               "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}})
    assert line.startswith("TEXT_MESSAGE_APP")
    assert "!a1b2c3d4 -> ^all" in line and "hops=0" in line and line.endswith(": hi")


# ---- connection handling --------------------------------------------------


class FakeSerialInterface:
    instances = []

    def __init__(self, devPath):
        self.devPath = devPath
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.nodesByNum = {ALICE: {"num": ALICE, "user": {"id": "!a1b2c3d4", "shortName": "ALB"}}}
        self.closed = False
        FakeSerialInterface.instances.append(self)

    def getMyNodeInfo(self):
        return {"user": {"id": "!a1b2c3d4", "longName": "Alice Base"}}

    def close(self):
        self.closed = True


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_reconnects_after_connection_loss(store, monkeypatch):
    FakeSerialInterface.instances = []
    monkeypatch.setattr(meshtastic.serial_interface, "SerialInterface", FakeSerialInterface)
    monkeypatch.setattr(meshtastic.util, "findPorts", lambda *_: ["/dev/ttyACM0"])

    stop = threading.Event()
    thread = threading.Thread(target=Collector(store, retry_seconds=0.05).run, args=(stop,))
    thread.start()
    try:
        assert wait_for(lambda: len(FakeSerialInterface.instances) == 1)
        FakeSerialInterface.instances[0].isConnected.clear()  # simulate unplug
        assert wait_for(lambda: len(FakeSerialInterface.instances) == 2)
    finally:
        stop.set()
        thread.join(5)

    assert not thread.is_alive()
    assert all(i.closed for i in FakeSerialInterface.instances)
    kinds = [e["kind"] for e in store.events()]
    assert kinds[:3] == ["connected", "disconnected", "connected"]
    assert store.nodes()[0]["short_name"] == "ALB"  # node DB snapshot taken on connect


def test_survives_connect_failures(store, monkeypatch):
    def broken(devPath):
        raise SystemExit("library gave up")

    ports = [[], ["/dev/ttyACM0"]]
    monkeypatch.setattr(meshtastic.serial_interface, "SerialInterface", broken)
    monkeypatch.setattr(meshtastic.util, "findPorts", lambda *_: ports[0] if len(ports) == 1 else ports.pop(0))

    stop = threading.Event()
    thread = threading.Thread(target=Collector(store, retry_seconds=0.05).run, args=(stop,))
    thread.start()
    assert wait_for(lambda: any(e["kind"] == "error" for e in store.events()))
    stop.set()
    thread.join(5)
    assert not thread.is_alive()


# ---- CLI ------------------------------------------------------------------


def test_cli_queries(radio, store, tmp_path, capsys):
    iface, _ = radio
    deliver(iface, store, make_packet(ALICE, portnums_pb2.TEXT_MESSAGE_APP, b"73 de N0CALL"), 1)
    db = str(tmp_path / "test.db")

    cli.main(["--db", db, "messages"])
    assert "73 de N0CALL" in capsys.readouterr().out
    cli.main(["--db", db, "nodes", "--since", "1h"])
    assert "!a1b2c3d4" in capsys.readouterr().out
    cli.main(["--db", db, "stats"])
    assert "TEXT_MESSAGE_APP" in capsys.readouterr().out
    cli.main(["--db", db, "packets", "--json"])
    assert json.loads(capsys.readouterr().out)["decoded"]["text"] == "73 de N0CALL"


def test_signal_columns_only_follow_direct_packets(store):
    def rx(pid, snr, rssi, hop_start, hop_limit, mqtt=False, local=False):
        store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": pid, "rxSnr": snr, "rxRssi": rssi,
                             "hopStart": hop_start, "hopLimit": hop_limit, "viaMqtt": mqtt,
                             "decoded": {"portnum": "POSITION_APP"}}, local=local)

    rx(1, 7.5, -80, 3, 3)  # heard directly
    node = store.node(BOB)
    assert (node["last_snr"], node["last_rssi"], node["hops_away"]) == (7.5, -80, 0)
    rx(2, -12.0, -118, 3, 1)  # relayed twice: the -12 dB belongs to the last relay
    node = store.node(BOB)
    assert (node["last_snr"], node["last_rssi"], node["hops_away"]) == (7.5, -80, 2)
    rx(3, 6.0, -60, 7, 2, mqtt=True)  # via an MQTT gateway: says nothing about hops or signal
    node = store.node(BOB)
    assert (node["last_snr"], node["hops_away"]) == (7.5, 2) and node["last_heard"] >= node["rf_heard"]


def test_local_reports_are_tagged_and_kept_out_of_stats(store):
    store.record_packet({"from": ALICE, "to": BROADCAST_NUM, "id": 1,
                         "decoded": {"portnum": "TELEMETRY_APP"}}, local=True)
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 2, "rxSnr": 1.0,
                         "decoded": {"portnum": "POSITION_APP"}})
    store.record_packet({"from": ALICE, "to": BROADCAST_NUM, "id": 3,  # logged before tagging existed
                         "decoded": {"portnum": "TELEMETRY_APP"}})
    store.mark_local(ALICE)
    counts, by_port = store.stats()
    assert counts["packets"] == 1 and counts["local reports"] == 2
    assert [r["portnum"] for r in by_port] == ["POSITION_APP"]
    assert store.heard_via() == {BOB: (1, 0)}


def test_migration_v2_recomputes_signal_columns(tmp_path):
    import sqlite3
    path = tmp_path / "v1.db"
    Store(path).close()
    conn = sqlite3.connect(path)  # turn it back into a v1 database with the old, contaminated values
    conn.executescript(f"""
        INSERT INTO nodes (num, node_id, first_seen, updated_at, last_snr, last_rssi, hops_away)
            VALUES ({BOB}, '!0badbeef', 0, 0, -15.0, -120, 7);
        INSERT INTO packets (logged_at, from_num, rx_snr, rx_rssi, hop_start, hop_limit, via_mqtt, json)
            VALUES (1, {BOB}, 8.0, -70, 3, 3, 0, '{{}}'),
                   (2, {BOB}, -15.0, -120, 3, 2, 0, '{{}}'),
                   (3, {BOB}, 5.0, -50, 7, 0, 1, '{{}}');
        PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()
    store = Store(path)
    node = store.node(BOB)
    assert (node["last_snr"], node["last_rssi"], node["hops_away"]) == (8.0, -70, 1)
    assert (node["direct_heard"], node["rf_heard"]) == (1, 2)
    store.close()


def test_exports(store):
    import io
    import xml.dom.minidom

    from meshshack.export import EXPORTS

    store.record_node_info({"num": BOB, "user": {"id": "!0badbeef", "shortName": "B&B", "longName": "Bob <base>"},
                            "position": {"latitude": 40.1100, "longitude": -88.2800}})
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 1, "decoded": {
        "portnum": "POSITION_APP", "position": {"latitude": 40.1165, "longitude": -88.2430}}})
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 2, "decoded": {
        "portnum": "TEXT_MESSAGE_APP", "text": 'hi, "mesh"'}})
    out = {}
    for (what, fmt), (exporter, _, _) in EXPORTS.items():
        buf = io.StringIO()
        exporter(store, buf)
        out[(what, fmt)] = buf.getvalue()
    assert 'hi, ""mesh""' in out[("messages", "csv")]  # CSV quoting
    for key in (("nodes", "kml"), ("positions", "gpx")):
        xml.dom.minidom.parseString(out[key])  # well-formed despite & and < in names
    assert "<name>B&amp;B</name>" in out[("positions", "gpx")]
    assert 'lat="40.1165"' in out[("positions", "gpx")]
