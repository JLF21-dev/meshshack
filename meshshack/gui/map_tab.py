"""Map tab: Leaflet (OpenStreetMap tiles) in an embedded browser, fed node positions from the database."""

import json
import time
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import __version__
from ..store import path_kind
from .common import WINDOWS, cell_size_text, distance_km, effective_precision, fmt_ago, precision_cell, since_for, station_position

MAP_HTML = Path(__file__).parent / "assets" / "map.html"
# OpenStreetMap's tile policy asks apps to identify themselves.
USER_AGENT = f"meshshack/{__version__} (personal Meshtastic desktop client)"


class MapPage(QWebEnginePage):
    def __init__(self, profile, on_action, parent=None):
        super().__init__(profile, parent)
        self.on_action = on_action

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith("meshshack:"):
            try:
                self.on_action(json.loads(message[len("meshshack:"):]))
            except ValueError:
                pass


class MapTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store
        self._ready = False
        self._dirty = True

        self.window_box = QComboBox()
        for label, seconds in WINDOWS:
            self.window_box.addItem(label, seconds)
        self.window_box.setCurrentIndex(int(win.settings.value("map/window", 1)))
        self.window_box.currentIndexChanged.connect(self._window_changed)
        self.trails = QCheckBox("Position trails")
        self.trails.setChecked(win.settings.value("map/trails", "false") == "true")
        self.trails.toggled.connect(self._trails_changed)
        self.areas = QCheckBox("Position areas")
        self.areas.setToolTip("Many nodes share only a rounded position. Show the box each one could be anywhere in.")
        self.areas.setChecked(win.settings.value("map/areas", "true") == "true")
        self.areas.toggled.connect(self._areas_changed)
        fit = QPushButton("Fit all nodes")
        fit.clicked.connect(lambda: self.view.page().runJavaScript("meshshack.fit()"))
        self.count = QLabel()

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show nodes heard:"))
        controls.addWidget(self.window_box)
        controls.addWidget(self.trails)
        controls.addWidget(self.areas)
        controls.addWidget(fit)
        controls.addStretch(1)
        controls.addWidget(self.count)

        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpUserAgent(USER_AGENT)
        self.view = QWebEngineView()
        page = MapPage(profile, self._on_action, self.view)
        page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setPage(page)
        self.view.loadFinished.connect(self._loaded)
        self.view.load(QUrl.fromLocalFile(str(MAP_HTML)))

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)

        win.dataChanged.connect(self._data_changed)
        win.statusChanged.connect(lambda _: self._data_changed())

    def _window_changed(self, index):
        self.win.settings.setValue("map/window", index)
        self._data_changed()

    def _trails_changed(self, on):
        self.win.settings.setValue("map/trails", "true" if on else "false")
        self._data_changed()

    def _areas_changed(self, on):
        self.win.settings.setValue("map/areas", "true" if on else "false")
        self._data_changed()

    def _loaded(self, ok):
        self._ready = ok
        self._dirty = True
        if self.isVisible():  # otherwise showEvent pushes; Leaflet can't fit bounds while hidden
            self._push()

    def _data_changed(self):
        self._dirty = True
        if self.isVisible():
            self._push()

    def showEvent(self, event):
        super().showEvent(event)
        self._push()

    def _push(self):
        if not self._ready or not self._dirty:
            return
        self._dirty = False
        self.view.page().runJavaScript(f"meshshack.update({json.dumps(self._payload())})")

    def _payload(self):
        since = since_for(self.window_box.currentData())
        my_num = self.win.my_num
        my_lat, my_lon, my_bits = station_position(self.win.status, self.store, my_num)

        heard_via = self.store.heard_via()
        reported_bits = self.store.position_precision()
        nodes = []
        for n in self.store.nodes():
            is_me = n["num"] == my_num
            lat, lon = (my_lat, my_lon) if is_me else (n["latitude"], n["longitude"])
            if lat is None or lon is None:
                continue
            if not is_me and since is not None and (n["last_heard"] or 0) < since:
                continue
            rf, mqtt = heard_via.get(n["num"], (0, 0))
            bits = my_bits if is_me else effective_precision(lat, lon, reported_bits.get(n["num"]))
            nodes.append({
                "path": path_kind(rf, mqtt), "rf_packets": rf, "mqtt_packets": mqtt,
                "cell": precision_cell(lat, lon, bits),
                "accuracy": f"Approximate: somewhere in a {cell_size_text(lat, bits)} area" if bits else "Exact",
                "approx_distance": bool(bits or my_bits),
                "num": n["num"], "id": n["node_id"], "short_name": n["short_name"], "long_name": n["long_name"],
                "hw_model": n["hw_model"], "role": n["role"], "lat": lat, "lon": lon, "is_me": is_me,
                "age_s": time.time() - n["last_heard"] if n["last_heard"] else None,
                "age_text": fmt_ago(n["last_heard"]), "snr": n["last_snr"], "rssi": n["last_rssi"],
                "hops": None if path_kind(rf, mqtt) == "mqtt" else n["hops_away"], "battery": n["battery_level"],
                "distance_km": None if is_me else distance_km(my_lat, my_lon, lat, lon),
            })
        self.count.setText(f"{len(nodes)} node{'s' if len(nodes) != 1 else ''} with a position")

        tracks = []
        if self.trails.isChecked():
            shown = {n["num"] for n in nodes}
            points = defaultdict(list)
            for p in self.store.position_tracks(since or 0):
                if p["from_num"] in shown:
                    points[p["from_num"]].append([p["latitude"], p["longitude"]])
            tracks = [{"num": num, "points": pts} for num, pts in points.items()]
        return {"nodes": nodes, "tracks": tracks, "show_areas": self.areas.isChecked()}

    def _on_action(self, msg):
        action, num = msg.get("action"), msg.get("num")
        if not isinstance(num, int):
            return
        if action == "dm":
            self.win.open_dm(num)
        elif action in ("traceroute", "position"):
            self.win.send_request(num, action)
