"""Visual check without a radio: fills a demo DB with fake nodes around the UIUC campus, runs the real API over a
fake radio, opens the GUI offscreen and saves a screenshot of each tab.

Usage: QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox .venv/bin/python dev/demo_gui.py OUTPUT_DIR
"""
import sys, time, json, shutil, os, tempfile
from pathlib import Path
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()  # keep demo settings out of ~/.config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from types import SimpleNamespace
from tests.test_hub import FakeInterface, ME
from meshshack.store import Store, BROADCAST_NUM
from meshshack.api import ApiServer, Radio

out = Path(sys.argv[1]); db_dir = out / "demo"; shutil.rmtree(db_dir, ignore_errors=True)
db = db_dir / "meshshack.db"
store = Store(db)
now = time.time()
nodes = [  # num, short, long, hw, role, lat, lon, age_s, snr, rssi, hops, batt
    (ME, "N0C", "N0CALL Base", "HELTEC_V4", "CLIENT_BASE", 40.1149, -88.2281, 5, None, None, 0, 100),
    (0x11111111, "CMP", "Campus Router", "RAK4631", "ROUTER", 40.1020, -88.2272, 120, 7.25, -88, 0, 96),
    (0x22222222, "SOL", "Solar Relay", "RAK4631", "ROUTER_LATE", 40.0900, -88.2400, 900, 2.5, -104, 1, 81),
    (0x33333333, "MOB", "Mobile One", "TBEAM", "CLIENT", 40.1165, -88.2430, 2400, -4.75, -115, 1, 55),
    (0x44444444, "WST", "West Side Base", "HELTEC_V3", "CLIENT", 40.1100, -88.2800, 7200, -9.0, -121, 2, None),
    (0x55555555, "FAR", "Far Node", "STATION_G2", "CLIENT", 40.0500, -88.1500, 3 * 86400, None, None, 3, 100),
]
for num, short, long, hw, role, lat, lon, age, snr, rssi, hops, batt in nodes:
    store.record_node_info({"num": num, "user": {"id": f"!{num:08x}", "longName": long, "shortName": short, "hwModel": hw, "role": role},
                            "position": {"latitude": lat, "longitude": lon}, "lastHeard": now - age, "snr": snr, "hopsAway": hops,
                            "deviceMetrics": {"batteryLevel": batt} if batt else {}})
    if rssi and hops == 0:  # signal columns only describe nodes heard directly
        store._conn.execute("UPDATE nodes SET last_rssi=? WHERE num=?", (rssi, num)); store._conn.commit()
def rx(sender, text, age, to=BROADCAST_NUM, ch=0, snr=6.0, rssi=-95, hs=3, hl=2):
    store.record_packet({"from": sender, "to": to, "id": int(now - age), "channel": ch, "rxSnr": snr, "rxRssi": rssi, "hopStart": hs, "hopLimit": hl,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text}}, now=now - age)
rx(0x11111111, "Good morning mesh! The campus router is back up after the firmware update.", 26 * 3600, snr=7.25, rssi=-88, hl=3)
rx(0x33333333, "Copy, hearing you 1 hop via SOL from the quad.", 25 * 3600, snr=-4.75, rssi=-115)
store.record_outgoing_message(9001, ME, BROADCAST_NUM, 0, "N0CALL base station on the air. 73!", now=now - 3000)
store._conn.execute("UPDATE messages SET status='relayed' WHERE packet_id=9001"); store._conn.commit()
rx(0x22222222, "Welcome! Your base is 2.5 dB SNR here at the relay.", 2500, snr=2.5, rssi=-104)
rx(0x44444444, "Heard you from the west side too, 2 hops.", 2000, snr=-9.0, rssi=-121, hs=3, hl=1)
store.record_packet({"from": 0x22222222, "to": BROADCAST_NUM, "id": 9101, "channel": 0, "rxSnr": 2.5, "rxRssi": -104,
                     "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "👋", "replyId": 9001, "emoji": 1}},
                    now=now - 2900)
store.record_packet({"from": 0x11111111, "to": BROADCAST_NUM, "id": 9102, "channel": 0, "rxSnr": 7.25, "rxRssi": -88,
                     "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "👍", "replyId": 9001, "emoji": 1}},
                    now=now - 2800)
store.record_outgoing_message(9002, ME, BROADCAST_NUM, 0, "Thanks all, logging everything to SQLite now.", now=now - 60)
store.record_packet({"from": 0x11111111, "to": BROADCAST_NUM, "id": 9103, "channel": 0, "rxSnr": 7.0, "rxRssi": -88,
                     "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TEXT_MESSAGE_APP",
                     "text": "Nice! Does it keep the encrypted packets too?", "replyId": 9002}}, now=now - 30)
rx(0x33333333, "Are you coming to the mesh meetup Saturday?", 600, to=ME, snr=-4.5, rssi=-114)
store.record_outgoing_message(9003, ME, 0x33333333, 0, "Planning on it, I'll bring the Heltec.", now=now - 500)
store._conn.execute("UPDATE messages SET status='delivered' WHERE packet_id=9003"); store._conn.commit()
rx(0x44444444, "Anyone on the admin channel?", 400, ch=1)
for i in range(6):
    store.record_packet({"from": 0x33333333, "to": BROADCAST_NUM, "id": 5000 + i, "decoded": {"portnum": "POSITION_APP",
        "position": {"latitude": 40.11 + i * 0.004, "longitude": -88.24 + i * 0.004}}}, now=now - 3000 + i * 400)

# A day of history for the charts: this station's own once-a-minute reports (sampled every
# 10 minutes here), and a solar router whose battery charges by day and is heard directly.
import math, random
random.seed(73)
for k in range(144):
    t = now - (143 - k) * 600
    load = 6 + 3 * math.sin(k / 144 * 2 * math.pi * 2) + random.uniform(-1.5, 1.5) + (9 if k in (61, 62) else 0)
    store.record_packet({"from": ME, "to": BROADCAST_NUM, "id": 20000 + k, "decoded": {"portnum": "TELEMETRY_APP",
        "telemetry": {"deviceMetrics": {"batteryLevel": 101, "voltage": round(4.18 + random.uniform(-0.01, 0.01), 3),
                                        "channelUtilization": round(max(load, 0.5), 2),
                                        "airUtilTx": round(1.2 + 0.4 * math.sin(k / 20) + random.uniform(0, 0.2), 2)}}}},
        now=t, local=True)
for h in range(24):
    t = now - (23 - h) * 3600
    sun = max(0.0, math.sin((h - 6) / 12 * math.pi))
    store.record_packet({"from": 0x11111111, "to": BROADCAST_NUM, "id": 30000 + h, "rxSnr": round(6 + random.uniform(-2.5, 2), 2),
        "rxRssi": -90 + random.randint(-4, 4), "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TELEMETRY_APP",
        "telemetry": {"deviceMetrics": {"batteryLevel": int(78 + 20 * sun), "voltage": round(3.85 + 0.3 * sun, 3),
                                        "channelUtilization": round(5 + random.uniform(-1, 2), 2), "airUtilTx": round(0.8 + random.uniform(0, 0.4), 2)}}}},
        now=t)
store.set_node_flags(0x11111111, favorite=True)
# Traffic from farther away, as the Coverage tab sees it: mostly relayed by the campus router
# (ID ends 0x11), some by the solar relay (0x22), a little via MQTT; plus direct packets from SOL.
for i in range(52):
    relay = 0x11 if i < 40 else 0x22
    store.record_packet({"from": 0x70000000 + i % 9, "to": BROADCAST_NUM, "id": 40000 + i, "rxSnr": round(random.uniform(-12, 4), 2),
                         "rxRssi": random.randint(-118, -90), "hopStart": 3, "hopLimit": random.choice([0, 1, 2]),
                         "relayNode": relay, "decoded": {"portnum": "POSITION_APP"}}, now=now - random.uniform(0, 86000))
for i in range(5):
    store.record_packet({"from": 0x7A000000 + i, "to": BROADCAST_NUM, "id": 41000 + i, "rxSnr": 5.0, "hopStart": 7,
                         "hopLimit": 4, "viaMqtt": True, "relayNode": 0x11, "decoded": {"portnum": "NODEINFO_APP"}},
                        now=now - random.uniform(0, 86000))
for i in range(8):
    store.record_packet({"from": 0x22222222, "to": BROADCAST_NUM, "id": 42000 + i, "rxSnr": round(random.uniform(-14, -8), 2),
                         "rxRssi": -110, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "POSITION_APP"}},
                        now=now - random.uniform(0, 86000))

# An emergency, as the logger would have detected it (the demo plays no alarm).
from meshshack import alerts
sos = {"from": 0x33333333, "to": BROADCAST_NUM, "id": 43000, "channel": 0, "rxSnr": -6.5, "rxRssi": -112, "hopStart": 3,
       "hopLimit": 2, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "SOS - rolled my ankle at the trailhead, need a ride"}}
alerts.check(store, sos, store.record_packet(sos, now=now - 120), now=now - 120)

# Two automation jobs and their recent runs (the demo doesn't run the scheduler).
from meshshack.automation import PRESETS
nist = store.save_automation_job({**PRESETS["Weekly NIST time check"], "name": "Weekly NIST time check",
                                  "destination": {"channel": 0}}, now=now - 30 * 86400)
wx = store.save_automation_job({**PRESETS["Daily weather"], "name": "Daily weather", "destination": {"channel": 0},
                                "dry_run": False}, now=now - 30 * 86400)
store.record_automation_run(nist, now - 2 * 86400, "dry run", "NIST time check 2026-09-27 17:03 UTC: this station's "
                            "clock is +1 ms off. Check yours!", "dry run: nothing sent", now=now - 2 * 86400)
store.record_automation_run(wx, now - 86400, "sent", "Weather Today: Sunny, 71°F, wind 5 mph SW. (NWS)", None,
                            now=now - 86400 + 240)
store.record_automation_run(wx, now - 3600, "skipped", None, "Paused: the channel is busy (24% utilization, limit 20%)",
                            now=now - 3600 + 300)
quiet = store.save_automation_job({**PRESETS["A favorite went quiet (notify me)"], "name": "A favorite went quiet"},
                                  now=now - 30 * 86400)
store.record_automation_run(quiet, now - 5400, "notified", "SOL (Solar Relay) hasn't been heard for 6 h.", None,
                            now=now - 5400, subject=0x22222222)

# An app with the settings permission, and one change it's waiting on you to approve.
store.create_token("home-dashboard", {"read", "config"})
store.request_approval("home-dashboard", "POST", "/api/config/position",
                       {"broadcast_secs": 3600, "smart_enabled": False, "gps_mode": "NOT_PRESENT"},
                       "Change position settings: broadcast every 3600 s, smart broadcast off, GPS NOT_PRESENT",
                       now=now - 300)

iface = FakeInterface()
iface.localNode.localConfig.device.role = 12  # CLIENT_BASE
iface.localNode.localConfig.position.position_broadcast_secs = 900
iface.localNode.localConfig.position.gps_mode = 2
orig = iface.getMyNodeInfo
iface.getMyNodeInfo = lambda: {**orig(), "deviceMetrics": {"batteryLevel": 100, "voltage": 4.18, "channelUtilization": 8.4, "airUtilTx": 1.2, "uptimeSeconds": 93784}}
api = ApiServer(Radio(SimpleNamespace(iface=iface, connected_port="/dev/ttyACM0"), store), db_dir / "hub.json", port=0)
api.start()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from meshshack.gui.app import MainWindow
app = QApplication([])
win = MainWindow(db); win.resize(1200, 820)
win.settings.setValue("alerts/sound", "false"); win.settings.setValue("alerts/raise", "false")
win.show()
steps = []
def shot(name):
    win.grab().save(str(out / f"gui_{name}.png")); print("saved", name)
def seq(i=0):
    plan = [(0, "chat"), (1, "map"), (2, "nodes"), (3, "coverage"), (4, "channels"), (5, "alerts"), (6, "automation"), (7, "other_apps"), (8, "device")]
    if i < len(plan):
        idx, name = plan[i]
        win.tabs.setCurrentIndex(idx)
        # The demo SOS is open only for the chat and alerts shots, so the banner isn't on every screenshot.
        store._conn.execute("UPDATE alerts SET acknowledged_at = ?", (None if name in ("chat", "alerts") else now,))
        store._conn.commit()
        win.alert_center.check()
        if name == "nodes":  # show the solar router's details and charts
            for r in range(win.nodes.table.rowCount()):
                if win.nodes.table.item(r, 0).data(0x0100) == 0x11111111:
                    win.nodes.table.selectRow(r)
            win.nodes.detail_tabs.setCurrentIndex(1)
        if name == "device":  # scroll down to this station's history
            from PySide6.QtWidgets import QScrollArea
            QTimer.singleShot(800, lambda: (lambda b: b.setValue(b.maximum()))(win.device.findChild(QScrollArea).verticalScrollBar()))
        QTimer.singleShot(5000 if name in ("map", "nodes", "device") else 1500, lambda: (shot(name), seq(i + 1)))
    else:
        # DM view + send via real API path
        win.open_dm(0x33333333); win.chat.input.setText("See you there!")
        QTimer.singleShot(1500, lambda: (win.chat._send(), QTimer.singleShot(2500, lambda: (shot("dm"), print("sent:", iface.sent[-1][:2]), app.quit()))))
QTimer.singleShot(3000, seq)
QTimer.singleShot(60000, app.quit)
app.exec()
api.stop()
