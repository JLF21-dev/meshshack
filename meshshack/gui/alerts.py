"""Emergency alerts in the app: a banner on every tab, an alarm sound, a tray notification, and
the Alerts tab (settings and history). Detection happens in the logger (../alerts.py); this only
presents what it recorded. Nothing here transmits.
"""

import math
import os
import struct
import time
import wave
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import alerts
from .common import fmt_ago, node_name

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "meshshack"


def alarm_sound_path():
    """A two-tone alarm with a pause, written on first use (so there's no audio file in the repo)."""
    path = CACHE_DIR / "alarm.wav"
    if path.exists():
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rate, frames = 22050, bytearray()
    for i in range(6):  # 6 beeps alternating 960/720 Hz, 0.18 s each, with soft edges
        freq, n = (960 if i % 2 == 0 else 720), int(rate * 0.18)
        for k in range(n):
            edge = min(1.0, k / 200, (n - k) / 200)
            frames += struct.pack("<h", int(0.6 * 32767 * edge * math.sin(2 * math.pi * freq * k / rate)))
        frames += b"\x00\x00" * int(rate * 0.04)
    frames += b"\x00\x00" * int(rate * 1.4)  # then a pause before it repeats
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def sender(a):
    return a["from_short"] or a["from_id"] or "unknown node"


def describe(a):
    where = "via the internet (MQTT)" if a["via_mqtt"] else (
        "heard directly" if a["hops"] == 0 else f"{a['hops']} hop{'s' if a['hops'] != 1 else ''} away"
        if a["hops"] is not None else "over the radio")
    return f"{a['reason']} from {sender(a)}, {where}"


class AlertCenter(QWidget):
    """The red banner shown above the tabs while any loud alert is open, plus the alarm."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store
        self._announced = set()  # alert ids this session has already sounded and notified for
        self._muted = False
        self.current = None
        self._sound = None

        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.open_button = QPushButton("Open conversation")
        self.open_button.clicked.connect(self._open)
        self.ack_button = QPushButton("Acknowledge")
        self.ack_button.clicked.connect(self._acknowledge)
        self.ack_all_button = QPushButton("Acknowledge all")
        self.ack_all_button.clicked.connect(lambda: self._acknowledge(all_open=True))
        self.mute_button = QPushButton("Mute sound")
        self.mute_button.clicked.connect(self.mute)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.label, 1)
        for b in (self.open_button, self.ack_button, self.ack_all_button, self.mute_button):
            layout.addWidget(b)
        self.setStyleSheet("AlertCenter { background: #b3261e; } QLabel { color: white; }")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()
        win.dataChanged.connect(self.check)

    # ---- state ----

    def open_alerts(self):
        return self.store.alerts(open_only=True, loud_only=True)

    def check(self):
        """Show the newest open loud alert; sound and notify once for each new one."""
        open_alerts = self.open_alerts()
        if not open_alerts:
            self.current = None
            self.hide()
            self._stop_sound()
            self._muted = False
            self._refresh_tray()
            return
        self.current = open_alerts[0]
        more = len(open_alerts) - 1
        self.label.setText(
            f"<b>🚨 Possible emergency: {escape(describe(self.current))}</b>, {fmt_ago(self.current['at'])}<br>"
            f"“{escape(self.current['text'] or '(no text)')}”"
            + (f"&nbsp;&nbsp;<i>+{more} more open</i>" if more else ""))
        self.ack_all_button.setVisible(more > 0)
        self.open_button.setVisible(self.current["from_num"] is not None or self.current["mc_message"] is not None)
        self.show()
        new = [a for a in open_alerts if a["id"] not in self._announced]
        if new:
            self._announced.update(a["id"] for a in new)
            self._announce(new[0])
        self._refresh_tray()

    def _announce(self, alert):
        settings = self.win.settings
        self._muted = False
        if settings.value("alerts/sound", "true") == "true":
            self._play()
        tray = self.win.tray
        if tray is not None:
            tray.showMessage("MeshShack: possible emergency", f"{describe(alert)}\n{alert['text']}",
                             QSystemTrayIcon.Critical, 30000)
        if settings.value("alerts/raise", "true") == "true":
            self.win.showNormal()
            self.win.raise_()
            self.win.activateWindow()
        QApplication.alert(self.win)  # flash in the taskbar if it isn't focused

    def _refresh_tray(self):
        if self.win.tray is not None:
            self.win.tray.refresh()

    # ---- sound ----

    def _play(self):
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:  # no multimedia support: the system beep will have to do
            QApplication.beep()
            return
        if self._sound is None:
            self._sound = QSoundEffect(self)
            self._sound.setSource(QUrl.fromLocalFile(str(alarm_sound_path())))
            self._sound.setLoopCount(QSoundEffect.Infinite)
            self._sound.setVolume(1.0)
        self._sound.play()

    def _stop_sound(self):
        if self._sound is not None:
            self._sound.stop()

    def mute(self):
        self._muted = True
        self._stop_sound()

    @property
    def sounding(self):
        return self._sound is not None and self._sound.isPlaying()

    # ---- actions ----

    def _open(self):
        a = self.current
        if a is None:
            return
        if a["mc_message"] is not None:  # a MeshCore message: its own conversation
            self._stop_sound()
            conv = ("mc_channel", a["channel"]) if a["channel"] is not None else ("mc_dm", a["mc_prefix"])
            self.win.chat.current = conv
            self.win.chat.refresh()
            self.win.tabs.setCurrentWidget(self.win.chat)
            return
        if a["from_num"] is None:
            return
        self._stop_sound()
        direct = a["to_num"] is not None and a["to_num"] != 0xFFFFFFFF and a["to_num"] == self.win.my_num
        if direct:
            self.win.open_dm(a["from_num"])
        else:
            self.win.chat.current = ("channel", a["channel"] or 0)
            self.win.chat.refresh()
            self.win.tabs.setCurrentWidget(self.win.chat)

    def _acknowledge(self, all_open=False):
        if all_open:
            self.store.acknowledge_alerts()
        elif self.current is not None:
            self.store.acknowledge_alerts([self.current["id"]])
        self.check()
        self.win.dataChanged.emit()


class AlertsTab(QWidget):
    """Settings for detection and the alarm, a test button, and the history of alerts."""

    COLUMNS = ["Time", "From", "Reason", "Heard", "Message", "Status"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store

        rules = alerts.rules(self.store)
        self.enabled = QCheckBox("Watch for possible emergencies")
        self.enabled.setChecked(rules["enabled"])
        self.keywords = QLineEdit(", ".join(rules["keywords"]))
        self.keywords.setToolTip("Whole words, any case; a phrase matches with any spacing between its words.")
        self.include_mqtt = QCheckBox("Also sound the alarm for alerts heard only via the internet (MQTT)")
        self.include_mqtt.setChecked(rules["include_mqtt"])
        self.sensors = QCheckBox("Treat detection-sensor messages as alerts")
        self.sensors.setChecked(rules["detection_sensors"])
        save = QPushButton("Save detection settings")
        save.clicked.connect(self._save_rules)
        self.sound = QCheckBox("Play an alarm until acknowledged")
        self.sound.setChecked(win.settings.value("alerts/sound", "true") == "true")
        self.sound.toggled.connect(lambda on: win.settings.setValue("alerts/sound", "true" if on else "false"))
        self.raise_window = QCheckBox("Bring the window to the front")
        self.raise_window.setChecked(win.settings.value("alerts/raise", "true") == "true")
        self.raise_window.toggled.connect(lambda on: win.settings.setValue("alerts/raise", "true" if on else "false"))
        test = QPushButton("Test alert")
        test.setToolTip("Records a test alert (clearly marked) so you can check the banner, sound and "
                        "notification. Nothing is sent.")
        test.clicked.connect(self._test)

        detection = QGroupBox("Detection (in the logger, so it works while the app is closed)")
        form = QFormLayout(detection)
        form.addRow(self.enabled)
        form.addRow("Keywords", self.keywords)
        form.addRow(self.include_mqtt)
        form.addRow(self.sensors)
        note = QLabel("Always flagged: Meshtastic alert messages and the alert bell. "
                      "MeshShack never sends anything in response.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        form.addRow(note)
        form.addRow(save)
        alarm = QGroupBox("In this app")
        alarm_layout = QVBoxLayout(alarm)
        alarm_layout.addWidget(self.sound)
        alarm_layout.addWidget(self.raise_window)
        alarm_layout.addWidget(test)
        alarm_layout.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(detection, 2)
        top.addWidget(alarm, 1)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        ack = QPushButton("Acknowledge selected")
        ack.clicked.connect(self._acknowledge_selected)
        history = QGroupBox("History")
        history_layout = QVBoxLayout(history)
        history_layout.addWidget(self.table)
        history_layout.addWidget(ack, 0, Qt.AlignLeft)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(history, 1)
        win.dataChanged.connect(self.refresh)
        self.refresh()

    def _save_rules(self):
        words = [w.strip() for w in self.keywords.text().split(",") if w.strip()]
        alerts.set_rules(self.store, enabled=self.enabled.isChecked(), keywords=words,
                         include_mqtt=self.include_mqtt.isChecked(), detection_sensors=self.sensors.isChecked())
        self.win.toast("Detection settings saved; the logger uses them from its next packet.")

    def _test(self):
        self.store.record_alert({
            "at": time.time(), "packet_row": None, "packet_id": None, "from_num": None, "to_num": None,
            "channel": None, "portnum": None, "reason": "Test (not from the mesh)",
            "text": "This is a test of MeshShack's emergency alert.", "via_mqtt": False, "loud": True})
        self.win.dataChanged.emit()

    def _acknowledge_selected(self):
        ids = [self.table.item(r.row(), 0).data(Qt.UserRole) for r in self.table.selectionModel().selectedRows()]
        if ids:
            self.store.acknowledge_alerts(ids)
            self.win.dataChanged.emit()

    def refresh(self):
        rows = self.store.alerts(limit=500)
        self.table.setRowCount(len(rows))
        for r, a in enumerate(rows):
            heard = "internet (MQTT)" if a["via_mqtt"] else ("directly" if a["hops"] == 0 else
                    f"{a['hops']} hop{'s' if a['hops'] != 1 else ''}" if a["hops"] is not None else "radio")
            status = (f"acknowledged {fmt_ago(a['acknowledged_at'])}" if a["acknowledged_at"] else
                      "OPEN" if a["loud"] else "open (quiet)")
            cells = [time.strftime("%b %-d %H:%M", time.localtime(a["at"])),
                     node_name(None, a["from_num"]) if a["from_num"] is not None and not a["from_short"] else
                     (a["from_short"] or "—"), a["reason"], heard, a["text"] or "", status]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, a["id"])
                self.table.setItem(r, c, item)
