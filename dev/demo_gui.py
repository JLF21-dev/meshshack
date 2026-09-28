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
    if rssi: store._conn.execute("UPDATE nodes SET last_rssi=? WHERE num=?", (rssi, num)); store._conn.commit()
def rx(sender, text, age, to=BROADCAST_NUM, ch=0, snr=6.0, rssi=-95, hs=3, hl=2):
    store.record_packet({"from": sender, "to": to, "id": int(now - age), "channel": ch, "rxSnr": snr, "rxRssi": rssi, "hopStart": hs, "hopLimit": hl,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text}}, now=now - age)
rx(0x11111111, "Good morning mesh! The campus router is back up after the firmware update.", 26 * 3600, snr=7.25, rssi=-88, hl=3)
rx(0x33333333, "Copy, hearing you 1 hop via SOL from the quad.", 25 * 3600, snr=-4.75, rssi=-115)
store.record_outgoing_message(9001, ME, BROADCAST_NUM, 0, "N0CALL base station on the air. 73!", now=now - 3000)
store._conn.execute("UPDATE messages SET status='relayed' WHERE packet_id=9001"); store._conn.commit()
rx(0x22222222, "Welcome! Your base is 2.5 dB SNR here at the relay.", 2500, snr=2.5, rssi=-104)
rx(0x44444444, "Heard you from the west side too, 2 hops.", 2000, snr=-9.0, rssi=-121, hs=3, hl=1)
store.record_outgoing_message(9002, ME, BROADCAST_NUM, 0, "Thanks all, logging everything to SQLite now.", now=now - 60)
rx(0x33333333, "Are you coming to the mesh meetup Saturday?", 600, to=ME, snr=-4.5, rssi=-114)
store.record_outgoing_message(9003, ME, 0x33333333, 0, "Planning on it, I'll bring the Heltec.", now=now - 500)
store._conn.execute("UPDATE messages SET status='delivered' WHERE packet_id=9003"); store._conn.commit()
rx(0x44444444, "Anyone on the admin channel?", 400, ch=1)
for i in range(6):
    store.record_packet({"from": 0x33333333, "to": BROADCAST_NUM, "id": 5000 + i, "decoded": {"portnum": "POSITION_APP",
        "position": {"latitude": 40.11 + i * 0.004, "longitude": -88.24 + i * 0.004}}}, now=now - 3000 + i * 400)

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
win = MainWindow(db); win.resize(1200, 780); win.show()
steps = []
def shot(name):
    win.grab().save(str(out / f"gui_{name}.png")); print("saved", name)
def seq(i=0):
    plan = [(0, "chat"), (1, "map"), (2, "nodes"), (3, "channels"), (4, "device")]
    if i < len(plan):
        idx, name = plan[i]
        win.tabs.setCurrentIndex(idx)
        QTimer.singleShot(5000 if name == "map" else 1500, lambda: (shot(name), seq(i + 1)))
    else:
        # DM view + send via real API path
        win.open_dm(0x33333333); win.chat.input.setText("See you there!")
        QTimer.singleShot(1500, lambda: (win.chat._send(), QTimer.singleShot(2500, lambda: (shot("dm"), print("sent:", iface.sent[-1][:2]), app.quit()))))
QTimer.singleShot(3000, seq)
QTimer.singleShot(60000, app.quit)
app.exec()
api.stop()
