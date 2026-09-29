"""Automation: scheduling, templates, sources, and the runner (through the real gatekeeper)."""

import time
from types import SimpleNamespace

import pytest

from meshshack import automation
from meshshack.airtime import Gatekeeper
from meshshack.api import ApiError, Radio
from meshshack.store import BROADCAST_NUM, Store

BOB = 0x0BADBEEF


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


# ---- scheduling ----

def test_slots():
    daily = {"type": "daily", "at": "07:00"}
    assert automation.latest_slot(daily, local(2026, 9, 28, 8, 0)) == local(2026, 9, 28, 7, 0)
    assert automation.latest_slot(daily, local(2026, 9, 28, 6, 59)) == local(2026, 9, 27, 7, 0)
    weekly = {"type": "weekly", "weekday": 6, "at": "12:00"}  # Sundays; Sep 27 2026 is a Sunday
    assert automation.latest_slot(weekly, local(2026, 9, 29, 9, 0)) == local(2026, 9, 27, 12, 0)
    assert automation.next_slot(weekly, local(2026, 9, 29, 9, 0)) == local(2026, 10, 4, 12, 0)
    every = {"type": "every", "hours": 6}
    assert automation.latest_slot(every, 1000 + 7 * 3600, anchor=1000) == 1000 + 6 * 3600
    assert automation.describe_trigger(weekly) == "Sundays at 12:00"
    j = automation.jitter(1, 12345)
    assert j == automation.jitter(1, 12345) and 0 <= j < 600


def test_trigger_validation():
    for bad in ({"type": "every", "hours": 2}, {"type": "daily", "at": "7am"}, {"type": "weekly", "at": "07:00"},
                {"type": "cron"}):
        with pytest.raises(ValueError):
            automation.validate_trigger(bad)


def job(**changes):
    base = {"name": "Test job", "trigger": {"type": "daily", "at": "07:00"}, "destination": {"channel": 0},
            "template": "hello {time}", "sources": []}
    return {**base, **changes}


def test_job_validation():
    automation.validate_job(job())
    for bad in (job(name=""), job(destination={"channel": 9}), job(template="   "),
                job(sources=[{"type": "http", "name": "weather", "url": "https://x"}]),  # reserved name
                job(sources=[{"type": "http", "name": "w", "url": "ftp://x"}]),
                job(sources=[{"type": "command", "name": "c", "command": " "}])):
        with pytest.raises(ValueError):
            automation.validate_job(bad)


# ---- templates and sources ----

class FakeSources:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]


def test_render():
    s = FakeSources({"weather.temp": 48.6, "station.nodes_heard_7d": 212, "x": "ok"})
    assert automation.render("{weather.temp:.0f}°F · {station.nodes_heard_7d} nodes {{literal}}", s) == \
        "49°F · 212 nodes {literal}"
    with pytest.raises(automation.RenderError, match="no value for {missing}"):
        automation.render("{x} {missing}", s)  # never sent half-filled
    with pytest.raises(automation.RenderError, match="limit is 200"):
        automation.render("{x}" + "a" * 250, s)  # never truncated
    with pytest.raises(automation.RenderError, match="format"):
        automation.render("{x:.2f}", s)


def test_builtin_sources(store):
    now = time.time()
    store.record_node_info({"num": BOB, "user": {"shortName": "BOB"}, "deviceMetrics": {"batteryLevel": 77}})
    for i in range(3):
        store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": i, "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3,
                             "decoded": {"portnum": "POSITION_APP"}}, now=now - 60)
    store.record_packet({"from": 0x1, "to": BROADCAST_NUM, "id": 9, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {
        "deviceMetrics": {"channelUtilization": 7.25, "batteryLevel": 101}}}}, local=True)
    src = automation.Sources(store, now=now)
    assert src.get("station.nodes_heard_24h") == 1 and src.get("station.packets_24h") == 3
    assert src.get("station.direct_neighbors_24h") == 1 and src.get("station.channel_util") == 7.25
    assert src.get("node.BOB.battery") == 77 and src.get("node.!0badbeef.name") == "BOB"
    with pytest.raises(automation.RenderError, match="position"):
        src.get("weather.temp")  # no station position yet
    with pytest.raises(automation.RenderError, match="commands are turned off"):
        automation.Sources(store, {"sources": [{"type": "command", "name": "t", "command": "date"}]}).get("cmd.t")


def test_http_source(store, monkeypatch):
    calls = []

    def fake_fetch(url, timeout=10):
        calls.append(url)
        return {"current": {"temp": 21.5, "list": [{"name": "first"}]}}

    monkeypatch.setattr(automation, "fetch_json", fake_fetch)
    automation.Sources._cache.clear()
    j = job(template="{wx.temp:.1f} {wx.first}", sources=[{"type": "http", "name": "wx", "url": "https://example.test/api",
                                                           "fields": {"temp": "current.temp", "first": "current.list.0.name"}}])
    assert automation.preview(store, j) == ("21.5 first", None)
    assert automation.preview(store, j)[0] == "21.5 first" and len(calls) == 1  # cached, not fetched again
    monkeypatch.setattr(automation, "fetch_json", lambda url, timeout=10: (_ for _ in ()).throw(OSError("timed out")))
    automation.Sources._cache.clear()
    text, problem = automation.preview(store, j)
    assert text is None and "timed out" in problem  # the run is skipped, not sent without it


# ---- the runner ----

class FakeIface:
    def __init__(self):
        self.isConnected = SimpleNamespace(is_set=lambda: True)
        self.myInfo = SimpleNamespace(my_node_num=0xA1B2C3D4)
        self.localNode = SimpleNamespace(channels=[SimpleNamespace(index=0, role=1)],
                                         localConfig=SimpleNamespace(lora=SimpleNamespace(hop_limit=3)))
        self.sent = []

    def getMyNodeInfo(self):
        return {}

    def sendData(self, data, destinationId=BROADCAST_NUM, **kw):
        self.sent.append((data.decode(), destinationId, kw))
        return SimpleNamespace(id=5000 + len(self.sent))


@pytest.fixture
def runner(store):
    clock = {"now": local(2026, 9, 28, 6, 0)}
    iface = FakeIface()
    radio = Radio(SimpleNamespace(iface=iface, connected_port="/dev/ttyACM0"), store)
    radio.gate = Gatekeeper(store, metrics=lambda: {}, clock=lambda: clock["now"])
    radio.status = lambda: {}
    auto = automation.Automation(store, radio, clock=lambda: clock["now"])
    return auto, clock, iface


def test_dry_run_then_live(store, runner):
    auto, clock, iface = runner
    job_id = store.save_automation_job(job(template="good morning {date}"), now=clock["now"] - 86400)
    assert store.automation_job(job_id)["dry_run"]  # new jobs start as dry runs

    clock["now"] = local(2026, 9, 28, 7, 11)  # past 07:00 plus any jitter
    auto.tick()
    [run] = store.automation_runs()
    assert run["status"] == "dry run" and run["text"].startswith("good morning") and not iface.sent
    auto.tick()
    assert len(store.automation_runs()) == 1  # once per scheduled time

    j = store.automation_job(job_id)
    store.save_automation_job({**j, "dry_run": False})
    clock["now"] = local(2026, 9, 29, 7, 11)
    auto.tick()
    run = store.automation_runs()[0]
    assert run["status"] == "sent" and iface.sent[0][0].startswith("good morning")
    log = store.tx_log()[0]
    assert log["source"] == f"automation:job{job_id}" and log["kind"] == "broadcast"


def test_skips_are_logged_never_retried(store, runner):
    auto, clock, iface = runner
    job_id = store.save_automation_job({**job(template="x {nope}"), "dry_run": False}, now=clock["now"] - 86400)
    clock["now"] = local(2026, 9, 28, 7, 11)
    auto.tick()
    assert store.automation_runs()[0]["status"] == "skipped" and "no value for {nope}" in store.automation_runs()[0]["detail"]

    store.save_automation_job({**store.automation_job(job_id), "template": "fine"})
    auto.radio.gate.set_transmit_enabled(False)
    clock["now"] = local(2026, 9, 29, 7, 11)
    auto.tick()
    assert "turned off" in store.automation_runs()[0]["detail"] and not iface.sent
    auto.tick()
    assert len(store.automation_runs()) == 2  # not retried

    clock["now"] = local(2026, 9, 30, 9, 30)  # the logger was down at 07:00: too late now
    auto.radio.gate.set_transmit_enabled(True)
    auto.tick()
    assert store.automation_runs()[0]["status"] == "missed" and not iface.sent


def test_gatekeeper_limits_apply_to_jobs(store, runner):
    auto, clock, iface = runner
    for i, at in enumerate(("07:00", "07:20", "07:40", "08:00", "08:20")):
        store.save_automation_job({**job(name=f"job {i}", trigger={"type": "daily", "at": at}), "dry_run": False},
                                  now=clock["now"] - 86400)
    for step in range(0, 200, 5):  # walk through the morning
        clock["now"] = local(2026, 9, 28, 7, 0) + step * 60
        auto.tick()
    statuses = [r["status"] for r in store.automation_runs()]
    assert statuses.count("sent") == 4 and statuses.count("skipped") == 1  # 4 automated sends a day
    assert "daily limit" in next(r["detail"] for r in store.automation_runs() if r["status"] == "skipped")


def test_never_fires_for_times_before_the_job_existed(store, runner):
    auto, clock, iface = runner
    clock["now"] = local(2026, 9, 28, 9, 0)
    store.save_automation_job(job(), now=clock["now"])  # created after today's 07:00
    auto.tick()
    assert store.automation_runs() == []


def test_api_is_app_only(store, tmp_path):
    import json
    import urllib.error
    import urllib.request

    from meshshack.api import ApiServer

    server = ApiServer(Radio(SimpleNamespace(iface=None, connected_port=None), store), tmp_path / "hub.json", port=0)
    server.start()
    owner = json.loads((tmp_path / "hub.json").read_text())["token"]
    other = store.create_token("tool", {"read", "send"})

    def call(method, path, body=None, token=owner):
        req = urllib.request.Request(server.url + path, method=method, headers={"Authorization": f"Bearer {token}"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    try:
        status, body = call("POST", "/api/automation/save", {**job(), "dry_run": False})
        assert status == 200 and store.automation_job(body["id"])["dry_run"]  # new jobs are dry runs regardless
        assert call("POST", "/api/automation/save", job(trigger={"type": "every", "hours": 1}))[0] == 400
        state = call("GET", "/api/automation")[1]
        assert state["jobs"][0]["schedule"] == "daily at 07:00" and "Daily weather" in state["presets"]
        preview = call("POST", "/api/automation/preview", job(template="hi {time}"))[1]
        assert preview["text"].startswith("hi ") and preview["limit"] == 200
        assert call("GET", "/api/automation", token=other)[0] == 403  # other apps can't see or change jobs
        assert call("POST", "/api/automation/save", job(name="x"), token=other)[0] == 403
    finally:
        server.stop()


# ---- event triggers ----

CAR, HUT, ME = 0x0CA20001, 0x0B070002, 0xA1B2C3D4


def heard(store, num, at, battery=None, short=None):
    store.record_packet({"from": num, "to": BROADCAST_NUM, "id": int(at) % 100000 + num % 97, "rxSnr": 1.0,
                         "hopStart": 3, "hopLimit": 2, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {
                             "deviceMetrics": {"batteryLevel": battery} if battery is not None else {}}}}, now=at)
    if short:
        store.record_node_info({"num": num, "user": {"shortName": short, "longName": f"{short} long"}})


def event_job(store, clock, trigger, template, dest=None, **extra):
    return store.save_automation_job({**job(name=f"ev {len(store.automation_jobs())}", trigger=trigger, template=template,
                                            destination=dest or {"notify": True}), **extra}, now=clock["now"])


def test_node_quiet_and_back(store, runner):
    auto, clock, iface = runner
    heard(store, CAR, clock["now"], short="CAR")
    event_job(store, clock, {"type": "node_quiet", "node": CAR, "hours": 6}, "{event.node} quiet for {event.quiet_for}")
    event_job(store, clock, {"type": "node_back", "node": CAR, "hours": 6}, "{event.node} back after {event.quiet_for}")
    auto.tick()
    assert store.automation_runs() == []  # first look: learn the state, don't fire
    clock["now"] += 7 * 3600
    auto.tick()
    auto.tick()
    [run] = store.automation_runs()
    assert run["status"] == "notified" and run["text"] == "CAR quiet for 7 h" and not iface.sent
    clock["now"] += 3 * 3600
    heard(store, CAR, clock["now"])
    auto.tick()
    back = store.automation_runs()[0]
    assert back["status"] == "notified" and back["text"] == "CAR back after 10 h"  # quiet 6 h + noticed + 3 h more
    auto.tick()
    assert len(store.automation_runs()) == 2  # once per change


def test_quiet_at_start_does_not_fire(store, runner):
    auto, clock, iface = runner
    heard(store, CAR, clock["now"] - 10 * 3600, short="CAR")
    event_job(store, clock, {"type": "node_quiet", "node": CAR, "hours": 6}, "{event.node} quiet")
    auto.tick()
    clock["now"] += 3600
    auto.tick()
    assert store.automation_runs() == []


def test_favorites_and_battery_hysteresis(store, runner):
    auto, clock, iface = runner
    for num, short in ((CAR, "CAR"), (HUT, "HUT")):
        heard(store, num, clock["now"], battery=60, short=short)
        store.set_node_flags(num, favorite=True)
    store.record_packet({"from": ME, "to": BROADCAST_NUM, "id": 1, "decoded": {"portnum": "TELEMETRY_APP"}}, local=True)
    store.set_node_flags(ME, favorite=True)  # this station is never its own subject
    event_job(store, clock, {"type": "battery_low", "node": "favorites", "below": 20}, "{event.node} at {event.battery}%")
    auto.tick()
    for battery, expect in ((18, 1), (19, 1), (25, 1), (31, 1), (15, 2), (101, 2)):
        clock["now"] += 3600
        heard(store, HUT, clock["now"], battery=battery)
        auto.tick()
        assert len(store.automation_runs()) == expect, battery
    assert [r["text"] for r in store.automation_runs()] == ["HUT at 15%", "HUT at 18%"]
    subjects = {row["subject"] for row in store._query("SELECT subject FROM automation_state")}
    assert subjects == {CAR, HUT}


def test_channel_busy_is_notify_only(store, runner):
    auto, clock, iface = runner
    with pytest.raises(ValueError, match="only notify"):
        automation.validate_job(job(trigger={"type": "channel_busy", "above": 25, "minutes": 15}))
    event_job(store, clock, {"type": "channel_busy", "above": 25, "minutes": 15}, "busy {event.channel_util:.0f}%")
    for i, util in enumerate([10, 11, 12, 10, 11, 30, 32, 34, 36, 38, 40]):  # a reading every 3 min
        clock["now"] += 180
        store.record_packet({"from": ME, "to": BROADCAST_NUM, "id": 900 + i, "decoded": {"portnum": "TELEMETRY_APP",
                             "telemetry": {"deviceMetrics": {"channelUtilization": util}}}}, now=clock["now"], local=True)
        auto.tick()
    [run] = store.automation_runs()
    assert run["status"] == "notified" and run["text"].startswith("busy ")


def test_notify_cooldown_and_sending_event_jobs(store, runner):
    auto, clock, iface = runner
    event_job(store, clock, {"type": "channel_busy", "above": 25, "minutes": 5}, "busy")

    def readings(*utils):
        for u in utils:
            clock["now"] += 100
            store.record_packet({"from": ME, "to": BROADCAST_NUM, "id": int(clock["now"]) % 100000,
                                 "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {
                                     "channelUtilization": u}}}}, now=clock["now"], local=True)
            auto.tick()

    readings(5, 5, 5, 40, 40, 40, 40)  # busy: notified
    readings(5, 5, 5, 5, 40, 40, 40, 40)  # ok, then busy again within 30 min: held off
    assert [r["status"] for r in store.automation_runs()].count("notified") == 1
    clock["now"] += 1800
    readings(5, 5, 5, 5, 40, 40, 40, 40)  # and again, past the cooldown: notified
    assert [r["status"] for r in store.automation_runs()].count("notified") == 2

    heard(store, HUT, clock["now"], short="HUT")
    sender = event_job(store, clock, {"type": "node_quiet", "node": HUT, "hours": 1}, "{event.node} is quiet",
                       dest={"channel": 0}, dry_run=False)
    auto.tick()
    clock["now"] += 3700
    auto.tick()
    run = store.automation_runs(job_id=sender)[0]
    assert run["status"] == "sent" and iface.sent[-1][0] == "HUT is quiet"
    assert store.tx_log()[0]["source"] == f"automation:job{sender}"  # through the gatekeeper


def test_changing_a_trigger_resets_its_state(store, runner):
    auto, clock, iface = runner
    heard(store, CAR, clock["now"], short="CAR")
    job_id = event_job(store, clock, {"type": "node_quiet", "node": CAR, "hours": 6}, "x")
    auto.tick()
    assert store.automation_state(job_id, CAR) is not None
    store.save_automation_job({**store.automation_job(job_id), "trigger": {"type": "node_quiet", "node": CAR, "hours": 2}})
    assert store.automation_state(job_id, CAR) is None


def test_event_preview_uses_a_sample(store):
    text, problem = automation.preview(store, {**automation.PRESETS["A favorite went quiet (notify me)"],
                                               "name": "p"})
    assert problem is None and text == "CMP (Campus Router) hasn't been heard for 7 h."
    assert automation.describe_trigger({"type": "battery_low", "node": "favorites", "below": 20}) == \
        "when a favorite's battery drops below 20%"


def test_app_shows_notify_runs(store, tmp_path):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meshshack.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    job_id = store.save_automation_job({**job(), "destination": {"notify": True}})
    store.record_automation_run(job_id, 1, "notified", "an old one", None)  # before the app started: not shown
    win = MainWindow(tmp_path / "test.db")
    try:
        store.record_automation_run(job_id, 2, "notified", "CAR hasn't been heard for 7 h.", None)
        win.dataChanged.emit()
        assert "CAR hasn't been heard" in win.statusBar().currentMessage()
    finally:
        win.close()
