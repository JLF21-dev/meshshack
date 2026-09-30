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
from ..coverage import report as coverage_report
from ..coverage import station_from_store
from ..meshcore_collector import TYPE_NAMES as MC_TYPE_NAMES
from ..paths import mc_via
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
        self.network_box = QComboBox()
        for label, value in (("Both networks", "both"), ("Meshtastic", "meshtastic"), ("MeshCore", "meshcore")):
            self.network_box.addItem(label, value)
        self.network_box.setToolTip("Meshtastic and MeshCore are separate meshes, heard through separate radios")
        index = self.network_box.findData(win.settings.value("map/network", "both"))
        self.network_box.setCurrentIndex(max(index, 0))
        self.network_box.currentIndexChanged.connect(self._network_changed)
        self.trails = QCheckBox("Position trails")
        self.trails.setChecked(win.settings.value("map/trails", "false") == "true")
        self.trails.toggled.connect(self._trails_changed)
        self.areas = QCheckBox("Position areas")
        self.areas.setToolTip("Many nodes share only a rounded position. Show the box each one could be anywhere in.")
        self.areas.setChecked(win.settings.value("map/areas", "true") == "true")
        self.areas.toggled.connect(self._areas_changed)
        self.links = QCheckBox("Direct links")
        self.links.setToolTip("Lines from this station to every node heard directly (0 hops) in the chosen time "
                              "range. Dashed when the node's position is rounded.")
        self.links.setChecked(win.settings.value("map/links", "false") == "true")
        self.links.toggled.connect(self._links_changed)
        fit = QPushButton("Fit all nodes")
        fit.clicked.connect(lambda: self.view.page().runJavaScript("meshshack.fit()"))
        self.count = QLabel()

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show"))
        controls.addWidget(self.network_box)
        controls.addWidget(QLabel("nodes heard:"))
        controls.addWidget(self.window_box)
        controls.addWidget(self.trails)
        controls.addWidget(self.areas)
        controls.addWidget(self.links)
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

    def _network_changed(self, index):
        self.win.settings.setValue("map/network", self.network_box.currentData())
        self._data_changed()

    def _trails_changed(self, on):
        self.win.settings.setValue("map/trails", "true" if on else "false")
        self._data_changed()

    def _links_changed(self, on):
        self.win.settings.setValue("map/links", "true" if on else "false")
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

        network = self.network_box.currentData()
        paths = self.win.paths()
        reported_bits = self.store.position_precision()
        nodes = []
        for n in self.store.nodes():
            is_me = n["num"] == my_num
            lat, lon = (my_lat, my_lon) if is_me else (n["latitude"], n["longitude"])
            if lat is None or lon is None:
                continue
            if not is_me and since is not None and (n["last_heard"] or 0) < since:
                continue
            if network == "meshcore" and not is_me:  # this station stays as the reference point
                continue
            p = paths.get(n["num"]) or {"kind": "unknown", "label": "Not logged yet", "why": "Only in the radio's "
                                        "saved node list: nothing from it has been logged yet"}
            bits = my_bits if is_me else effective_precision(lat, lon, reported_bits.get(n["num"]))
            nodes.append({
                "key": n["num"], "net": "meshtastic",
                "path": p["kind"], "path_label": p["label"], "path_why": p["why"],
                "cell": precision_cell(lat, lon, bits),
                "accuracy": f"Approximate: somewhere in a {cell_size_text(lat, bits)} area" if bits else "Exact",
                "approx_distance": bool(bits or my_bits),
                "num": n["num"], "id": n["node_id"], "short_name": n["short_name"], "long_name": n["long_name"],
                "hw_model": n["hw_model"], "role": n["role"], "lat": lat, "lon": lon, "is_me": is_me,
                "age_s": time.time() - n["last_heard"] if n["last_heard"] else None,
                "age_text": fmt_ago(n["last_heard"]), "snr": n["last_snr"], "rssi": n["last_rssi"],
                "hops": None if p["kind"] in ("mqtt", "inferred") else n["hops_away"], "battery": n["battery_level"],
                "distance_km": None if is_me else distance_km(my_lat, my_lon, lat, lon),
            })
        mesh_nodes = len(nodes) - any(n["is_me"] for n in nodes)
        mc_nodes = self._meshcore_nodes(since, my_lat, my_lon) if network != "meshtastic" else []
        nodes += mc_nodes
        parts = []
        if network != "meshcore":
            parts.append(f"{mesh_nodes} Meshtastic")
        if network != "meshtastic":
            parts.append(f"{len(mc_nodes)} MeshCore")
        self.count.setText(" + ".join(parts) + " with a position")

        tracks = []
        if self.trails.isChecked():
            shown = {n["num"] for n in nodes}
            points = defaultdict(list)
            for p in self.store.position_tracks(since or 0):
                if p["from_num"] in shown:
                    points[p["from_num"]].append([p["latitude"], p["longitude"]])
            tracks = [{"num": num, "points": pts} for num, pts in points.items() if network != "meshcore"]
        links = []
        if self.links.isChecked() and my_lat is not None:
            preset = (self.win.status.get("lora") or {}).get("modem_preset")
            rep = coverage_report(self.store, (my_lat, my_lon, my_bits), preset, since=since or 0)
            links = [{"num": n["num"], "name": n["short_name"] or n["id"], "lat": n["latitude"], "lon": n["longitude"],
                      "snr": n["snr_median"], "margin": n["margin_db"], "packets": n["packets"],
                      "rounded": n["rounded"], "distance": n["distance_km"]}
                     for n in rep["neighbors"] if n["latitude"] is not None and network != "meshcore"]
        return {"nodes": nodes, "tracks": tracks, "show_areas": self.areas.isChecked(),
                "station": [my_lat, my_lon] if my_lat is not None else None, "links": links}

    def _meshcore_nodes(self, since, my_lat, my_lon):
        station = station_from_store(self.store)
        out = []
        for n in self.store.mc_nodes():
            if n["latitude"] is None or n["longitude"] is None:
                continue
            if since is not None and (n["last_heard"] or 0) < since:
                continue
            kind, label, why = mc_via(n, station)
            out.append({
                "key": "mc:" + n["public_key"], "net": "meshcore", "path": kind, "path_label": label,
                "path_why": why, "cell": None, "accuracy": "As set by its owner", "approx_distance": False,
                "id": n["public_key"][:12], "short_name": n["name"] or n["public_key"][:8], "long_name": None,
                "type": MC_TYPE_NAMES.get(n["type"], "unknown"), "lat": n["latitude"], "lon": n["longitude"],
                "is_me": False, "age_s": time.time() - n["last_heard"] if n["last_heard"] else None,
                "age_text": fmt_ago(n["last_heard"]), "snr": n["last_snr"], "rssi": n["last_rssi"],
                "hops": n["min_hops"], "adverts": n["adverts"],
                "distance_km": distance_km(my_lat, my_lon, n["latitude"], n["longitude"]),
            })
        return out

    def _on_action(self, msg):
        action, num = msg.get("action"), msg.get("num")
        if not isinstance(num, int):
            return
        if action == "dm":
            self.win.open_dm(num)
        elif action in ("traceroute", "position"):
            self.win.send_request(num, action)
