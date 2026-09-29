"""Emergency detection (in the logger) and how the app presents it. Nothing is ever transmitted."""

import os
import tempfile

import pytest

from meshshack import alerts
from meshshack.store import BROADCAST_NUM, Store

ME, BOB = 0xA1B2C3D4, 0x0BADBEEF


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def text(body, sender=BOB, **extra):
    return {"from": sender, "to": BROADCAST_NUM, "id": 1, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": body},
            **extra}


def test_keywords_are_whole_words_and_phrases():
    rules = dict(alerts.DEFAULT_RULES)
    assert alerts.detect(text("SOS stuck on the trail"), rules) == "Keyword: SOS"
    assert alerts.detect(text("mayday mayday"), rules) == "Keyword: MAYDAY"
    assert alerts.detect(text("please HELP   me, fell"), rules) == "Keyword: HELP ME"
    assert alerts.detect(text("call 911"), rules) == "Keyword: 911"
    assert alerts.detect(text("SOS-please"), rules) == "Keyword: SOS"  # hyphens end words: err toward alerting
    for harmless in ("Sosa's node is back", "SOSO", "helpme.com", "911x", "the emergencyroom node"):
        assert alerts.detect(text(harmless), rules) is None, harmless
    rules["keywords"] = ["fire"]
    assert alerts.detect(text("SOS"), rules) is None and alerts.detect(text("Fire on the ridge"), rules) == "Keyword: FIRE"


def test_alert_types_and_what_is_ignored():
    rules = dict(alerts.DEFAULT_RULES)
    assert alerts.detect(text("\x07 need a hand"), rules) == "Alert bell"
    alert_app = {"from": BOB, "to": BROADCAST_NUM, "id": 2, "decoded": {"portnum": "ALERT_APP", "payload": b"Flood!"}}
    assert alerts.detect(alert_app, rules) == "Alert message"
    sensor = {"from": BOB, "to": BROADCAST_NUM, "id": 3, "decoded": {"portnum": "DETECTION_SENSOR_APP", "payload": b"x"}}
    assert alerts.detect(sensor, rules) is None
    assert alerts.detect(sensor, {**rules, "detection_sensors": True}) == "Detection sensor"
    reaction = text("🆘", decoded=None)
    reaction["decoded"] = {"portnum": "TEXT_MESSAGE_APP", "text": "SOS", "emoji": 1, "replyId": 5}
    assert alerts.detect(reaction, rules) is None  # a reaction isn't a message
    assert alerts.detect(text("SOS"), rules, local=True) is None  # this station's own
    assert alerts.detect(text("SOS"), {**rules, "enabled": False}) is None


def test_check_records_alerts_and_mqtt_ones_are_quiet(store):
    row = store.record_packet(text("MAYDAY engine out"))
    alert = alerts.check(store, text("MAYDAY engine out"), row)
    assert alert["loud"] and alert["reason"] == "Keyword: MAYDAY"
    quiet = alerts.check(store, text("SOS", viaMqtt=True), row)
    assert not quiet["loud"]  # far away via the internet: recorded, no alarm
    alerts.set_rules(store, include_mqtt=True)
    assert alerts.check(store, text("SOS", viaMqtt=True), row)["loud"]
    assert [a["reason"] for a in store.alerts(open_only=True, loud_only=True)] == ["Keyword: SOS", "Keyword: MAYDAY"]
    assert store.acknowledge_alerts([alert["id"]]) == 1
    assert store.acknowledge_alerts() == 2 and not store.alerts(open_only=True)


def test_alert_app_messages_show_in_chat(store):
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 7, "channel": 0,
                         "decoded": {"portnum": "ALERT_APP", "payload": b"Bridge out on Route 9"}})
    [m] = store.thread(channel=0)
    assert m["text"] == "Bridge out on Route 9" and m["portnum"] == "ALERT_APP"


def test_alerts_api(store, tmp_path):
    import json
    import urllib.error
    import urllib.request
    from types import SimpleNamespace

    from meshshack.api import ApiServer, Radio

    alerts.check(store, text("SOS"), None)
    server = ApiServer(Radio(SimpleNamespace(iface=None, connected_port=None), store), tmp_path / "hub.json", port=0)
    server.start()
    owner = json.loads((tmp_path / "hub.json").read_text())["token"]
    reader = store.create_token("dashboard", {"read"})

    def call(method, path, body=None, token=owner):
        req = urllib.request.Request(server.url + path, method=method, headers={"Authorization": f"Bearer {token}"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    try:
        status, body = call("GET", "/api/alerts?open=1", token=reader)
        assert status == 200 and body["alerts"][0]["reason"] == "Keyword: SOS"
        assert call("POST", "/api/alerts/ack", {"all": True}, token=reader)[0] == 403  # only the app
        assert call("POST", "/api/alerts/ack", {"all": True})[1] == {"acknowledged": 1}
    finally:
        server.stop()


def test_banner_tray_and_test_button(store, tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="meshshack-test-config-"))
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow
    from meshshack.gui.tray import Tray, state_icon

    app = QApplication.instance() or QApplication([])
    assert not state_icon(0, alert=True).isNull()
    win = MainWindow(tmp_path / "test.db")
    win.settings.setValue("alerts/sound", "false")  # no alarm in tests
    win.settings.setValue("alerts/raise", "false")
    win.tray = Tray(win, lambda: None)
    try:
        win.show()
        app.processEvents()
        assert not win.alert_center.isVisible()
        store.record_node_info({"num": BOB, "user": {"shortName": "BOB"}})
        alerts.check(store, text("SOS lost near the creek"), None)
        win.dataChanged.emit()
        center = win.alert_center
        assert center.isVisible() and "Keyword: SOS from BOB" in center.label.text()
        assert "Possible emergency" in win.tray.toolTip()
        center._acknowledge()
        assert not center.isVisible() and "Possible emergency" not in win.tray.toolTip()

        win.alerts_tab._test()  # the test button: a clearly marked alert, nothing sent
        assert center.isVisible() and "Test (not from the mesh)" in center.label.text()
        assert win.alerts_tab.table.rowCount() == 2
        center._acknowledge(all_open=True)
        assert not center.isVisible()
    finally:
        win.quitting = True
        win.close()


def test_flagged_messages_are_marked_in_chat(store):
    row = store.record_packet(text("MAYDAY taking on water"))
    alerts.check(store, text("MAYDAY taking on water"), row)
    store.record_packet({**text("all good here"), "id": 9})
    by_text = {m["text"]: m["alert_reason"] for m in store.thread(channel=0)}
    assert by_text == {"MAYDAY taking on water": "Keyword: MAYDAY", "all good here": None}
