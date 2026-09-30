"""Radios on USB: hardware IDs, probing replies, links, and how the logger picks its radio."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import mesh_pb2

from meshshack import devices
from meshshack.store import Store

MT, MC = "240AC4000001", "240AC4000002"
real_scan = devices.scan  # tests replace devices.scan; fake_ports still needs the real one

# A MeshCore companion's reply to app start (SELF_INFO), laid out as the firmware sends it
# (made-up key and name): code 5, advert type, TX power, max TX power, 32-byte public key,
# 12 bytes of position and flags, frequency and bandwidth in kHz x 1000, SF, CR, name.
SELF_INFO = (bytes([5, 1, 10, 22]) + bytes(range(0xA0, 0xC0)) + bytes(12) + (910525).to_bytes(4, "little")
             + (62500).to_bytes(4, "little") + bytes([7, 5]) + b"TESTNODE")
MESHCORE_REPLY = b">" + len(SELF_INFO).to_bytes(2, "little") + SELF_INFO


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def port(device, serial, description="radio", vid=0x303A):
    return SimpleNamespace(device=device, serial_number=serial, vid=vid, pid=0x1001, description=description,
                           product=None, location="1-1")


def fake_ports(*ports):
    return real_scan(list_ports=lambda: list(ports))


def test_hardware_id():
    assert devices.hardware_id("24:0A:C4:00:00:02") == devices.hardware_id("240ac4000002") == MC
    assert devices.hardware_id(None) is None and devices.hardware_id("::") is None


def test_scan_skips_builtin_ports():
    rows = fake_ports(port("/dev/ttyS0", None, vid=None), port("/dev/ttyACM1", "24:0A:C4:00:00:02"),
                      port("/dev/ttyACM0", MT))
    assert [(r["port"], r["hardware_id"]) for r in rows] == [("/dev/ttyACM0", MT), ("/dev/ttyACM1", MC)]


def test_parse_replies():
    [frame] = devices.meshcore_frames(b"noise" + MESHCORE_REPLY)
    info = devices.meshcore_self_info(frame)
    assert info["name"] == "TESTNODE" and info["public_key"].startswith("a0a1a2")
    assert (info["freq_mhz"], info["bw_khz"], info["sf"], info["cr"]) == (910.525, 62.5, 7, 5)
    assert devices.meshcore_self_info(b"\x00") is None

    my_info = mesh_pb2.FromRadio(my_info=mesh_pb2.MyNodeInfo(my_node_num=0xA1B2C3D4)).SerializeToString()
    stream = b"debug text\x94\xc3" + len(my_info).to_bytes(2, "big") + my_info
    [raw] = devices.meshtastic_frames(stream)
    assert mesh_pb2.FromRadio.FromString(raw).my_info.my_node_num == 0xA1B2C3D4


def test_links(store):
    devices.link(store, "24:0A:C4:00:00:01", "meshtastic", node="!a1b2c3d4")
    devices.link(store, MC, "meshcore")
    assert devices.linked(store, "meshtastic") == MT and devices.linked(store, "meshcore") == MC
    devices.link(store, "AAAA", "meshtastic")  # one radio per kind: this replaces the first
    assert devices.linked(store, "meshtastic") == "AAAA" and MT not in devices.links(store)
    devices.link(store, MC, "meshtastic")  # relinking a radio as another kind moves it
    assert devices.links(store) == {MC: devices.links(store)[MC]} and devices.linked(store, "meshcore") is None
    with pytest.raises(ValueError):
        devices.link(store, MT, "lora-thing")
    assert devices.unlink(store, MC) and not devices.unlink(store, MC)
    assert devices.links(store) == {}


def test_survey(store):
    devices.link(store, "CCCC", "meshcore")  # linked but not plugged in
    ports = fake_ports(port("/dev/ttyACM0", MT), port("/dev/ttyACM1", MC))
    probed = []

    def prober(p):
        probed.append(p)
        return {"kind": "meshcore", "node": "TESTNODE"}

    rows = devices.survey(store, probe_ports=True, skip=["/dev/ttyACM0"], ports=ports, prober=prober)
    assert probed == ["/dev/ttyACM1"]  # the logger's port is never probed
    by_id = {r["hardware_id"]: r for r in rows}
    assert by_id[MT]["in_use"] and "probe" not in by_id[MT]
    assert by_id[MC]["probe"]["kind"] == "meshcore"
    assert not by_id["CCCC"]["present"] and by_id["CCCC"]["link"]["kind"] == "meshcore"


@pytest.fixture
def collector(store, monkeypatch):
    import meshtastic.util

    from meshshack.collector import Collector

    state = {"ports": [port("/dev/ttyACM0", MT), port("/dev/ttyACM1", "24:0A:C4:00:00:02")]}
    monkeypatch.setattr(devices, "scan", lambda list_ports=None: fake_ports(*state["ports"]))
    # Both boards are ESP32s, so the Meshtastic library's own detection can't tell them apart.
    monkeypatch.setattr(meshtastic.util, "findPorts", lambda *a: [p.device for p in state["ports"]])
    return Collector(store), state


def test_logger_picks_its_radio(store, collector):
    c, state = collector
    assert c._find_port() is None  # two possibilities and nothing linked: it won't guess
    devices.link(store, MC, "meshcore")
    assert c._find_port() == "/dev/ttyACM0"  # the other one is spoken for
    devices.link(store, MT, "meshtastic")
    state["ports"] = [port("/dev/ttyACM3", MT), port("/dev/ttyACM1", MC)]  # moved to another socket
    assert c._find_port() == "/dev/ttyACM3"
    state["ports"] = [port("/dev/ttyACM1", MC)]  # unplugged: wait for it, don't take another
    assert c._find_port() is None
    c.port = "/dev/ttyUSB9"  # --port always wins
    assert c._find_port() == "/dev/ttyUSB9"


def test_logger_notices_a_new_link(store, collector):
    c, _ = collector
    c.connected_hwid = MT
    assert not c._link_changed()  # nothing linked
    devices.link(store, MT, "meshtastic")
    assert not c._link_changed()
    devices.link(store, "BBBB", "meshtastic")
    assert c._link_changed()


def test_radios_api_is_owner_only(store, tmp_path, monkeypatch):
    from meshshack.api import ApiServer, Radio

    monkeypatch.setattr(devices, "scan", lambda list_ports=None: fake_ports(port("/dev/ttyACM1", MC)))
    monkeypatch.setattr(devices, "probe", lambda p: {"kind": "meshcore", "node": "TESTNODE"})
    collector = SimpleNamespace(iface=None, connected_port=None, connected_hwid=None)
    server = ApiServer(Radio(collector, store), tmp_path / "hub.json", port=0)
    server.start()
    owner = json.loads((tmp_path / "hub.json").read_text())["token"]
    other = store.create_token("tool", {"read", "send", "config"})

    def call(method, path, body=None, token=owner):
        req = urllib.request.Request(server.url + path, method=method, headers={"Authorization": f"Bearer {token}"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    try:
        for method, path in [("GET", "/api/radios"), ("POST", "/api/radios/scan"), ("POST", "/api/radios/link"),
                             ("POST", "/api/radios/unlink")]:
            assert call(method, path, {"hardware_id": MC, "kind": "meshcore"}, token=other)[0] == 403, path
        code, body = call("POST", "/api/radios/scan", {})
        assert code == 200 and body["radios"][0]["probe"]["kind"] == "meshcore"
        assert call("POST", "/api/radios/link", {"hardware_id": MC, "kind": "meshcore"})[0] == 200
        assert call("GET", "/api/radios")[1]["radios"][0]["link"]["kind"] == "meshcore"
        assert call("POST", "/api/radios/link", {"hardware_id": MC, "kind": "nope"})[0] == 400
        assert call("POST", "/api/radios/unlink", {"hardware_id": MC})[0] == 200
        assert call("POST", "/api/radios/unlink", {"hardware_id": MC})[0] == 404
    finally:
        server.stop()


def test_radios_group(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(tmp_path / "test.db")
    try:
        group = win.device.radios
        import time
        deadline = time.time() + 5  # let the window's own first request (no logger here) finish first
        while not group.message.text() and time.time() < deadline:
            app.processEvents()
        group._probes[MC] = {"kind": "meshcore", "node": "TESTNODE"}
        group._loaded({"radios": [
            {"hardware_id": MT, "port": "/dev/ttyACM0", "present": True, "in_use": True, "connected": True,
             "link": {"kind": "meshtastic", "node": "!a1b2c3d4"}, "description": "heltec v4"},
            {"hardware_id": MC, "port": "/dev/ttyACM1", "present": True, "in_use": False, "link": None,
             "description": "USB JTAG/serial debug unit"},
            {"hardware_id": "CCCC", "port": None, "present": False, "in_use": False,
             "link": {"kind": "meshcore"}, "description": None},
        ]}, None)
        app.processEvents()
        cell = lambda r, c: group.table.item(r, c).text()  # noqa: E731
        assert (cell(0, 2), cell(0, 4)) == ("Meshtastic", "Meshtastic (connected now)")
        assert cell(1, 4) == "MeshCore TESTNODE" and cell(2, 1) == "Not plugged in"
        win.status = {"connected": True}
        group.table.selectRow(0)
        assert not group.link_mt.isEnabled() and not group.link_mc.isEnabled()  # it's the connected radio
        group.table.selectRow(1)
        assert group.link_mt.isEnabled() and group.link_mc.isEnabled() and not group.unlink_button.isEnabled()
    finally:
        win.close()
