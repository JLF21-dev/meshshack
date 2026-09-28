"""Device tab: radio status, reboot/announce, and owner/role/position settings."""

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

ROLE_HELP = {
    "CLIENT": "Default. Rebroadcasts packets when needed and works with the phone app.",
    "CLIENT_MUTE": "Never rebroadcasts. Good when several of your nodes sit close together.",
    "CLIENT_HIDDEN": "Only transmits when needed. For stealth or power saving.",
    "CLIENT_BASE": "Home base station: gives your favorited nodes' traffic router priority; otherwise like CLIENT.",
    "TRACKER": "Prioritizes sending its GPS position.",
    "SENSOR": "Prioritizes sending telemetry.",
    "ROUTER": "Infrastructure at a high, well-placed site; always rebroadcasts first. "
              "Too many routers hurt a mesh, so coordinate with the local group.",
    "ROUTER_LATE": "Rebroadcasts after other nodes have had their chance. Fills coverage gaps without "
                   "competing with routers; still worth coordinating locally.",
}


def _form_value_row(form, label):
    value = QLabel("—")
    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
    form.addRow(label, value)
    return value


class DeviceTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._populated_from = None  # config snapshot the forms were last filled from
        self._populating = False
        self._dirty = set()

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("padding: 8px; border-radius: 4px; background: #fce8b2; color: #3c2f00")

        content = QWidget()
        grid = QGridLayout(content)
        grid.addWidget(self._status_group(), 0, 0, 2, 1)
        grid.addWidget(self._actions_group(), 0, 1)
        grid.addWidget(self._owner_group(), 1, 1)
        grid.addWidget(self._role_group(), 2, 0)
        grid.addWidget(self._position_group(), 2, 1)
        grid.addWidget(self._history_group(), 3, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(self.banner)
        layout.addWidget(scroll, 1)

        win.statusChanged.connect(self._status_changed)
        self._status_changed(win.status)

    # ---- layout ----

    def _status_group(self):
        box = QGroupBox("Status")
        form = QFormLayout(box)
        self.fields = {}
        for key, label in [
            ("name", "Node"), ("id", "Node ID"), ("hw", "Hardware"), ("firmware", "Firmware"),
            ("licensed", "Licensed (ham) mode"), ("port", "USB port"), ("role", "Role"),
            ("region", "Region"), ("preset", "Modem preset"), ("hops", "Hop limit"), ("tx", "Transmit enabled"),
            ("battery", "Battery"), ("voltage", "Voltage"), ("chutil", "Channel utilization"),
            ("airtx", "Air time (TX)"), ("uptime", "Uptime"), ("channels", "Channels"),
        ]:
            self.fields[key] = _form_value_row(form, label)
        self.fields["channels"].setWordWrap(True)
        return box

    def _actions_group(self):
        box = QGroupBox("Actions")
        layout = QVBoxLayout(box)
        self.reboot_button = QPushButton("Reboot radio…")
        self.reboot_button.clicked.connect(self._reboot)
        self.announce_button = QPushButton("Announce node info")
        self.announce_button.setToolTip("Broadcast this node's name and hardware so others see it right away")
        self.announce_button.clicked.connect(self._announce)
        self.refresh_button = QPushButton("Discard unsaved changes")
        self.refresh_button.clicked.connect(self._reload_forms)
        for b in (self.reboot_button, self.announce_button, self.refresh_button):
            layout.addWidget(b)
        return box

    def _owner_group(self):
        box = QGroupBox("Owner")
        form = QFormLayout(box)
        self.long_name = QLineEdit()
        self.long_name.setMaxLength(39)
        self.short_name = QLineEdit()
        self.short_name.setMaxLength(4)
        for w in (self.long_name, self.short_name):
            w.textEdited.connect(lambda _: self._mark_dirty("owner"))
        form.addRow("Long name", self.long_name)
        form.addRow("Short name (≤ 4)", self.short_name)
        self.owner_apply = QPushButton("Apply owner…")
        self.owner_apply.clicked.connect(self._apply_owner)
        form.addRow("", self.owner_apply)
        return box

    def _role_group(self):
        box = QGroupBox("Role")
        layout = QVBoxLayout(box)
        self.role = QComboBox()
        self.role.activated.connect(lambda _: self._mark_dirty("role"))
        self.role.currentTextChanged.connect(lambda r: self.role_help.setText(ROLE_HELP.get(r, "")))
        self.role_help = QLabel()
        self.role_help.setWordWrap(True)
        self.role_apply = QPushButton("Apply role…")
        self.role_apply.clicked.connect(self._apply_role)
        layout.addWidget(self.role)
        layout.addWidget(self.role_help)
        layout.addWidget(self.role_apply)
        layout.addStretch(1)
        return box

    def _position_group(self):
        box = QGroupBox("Position")
        form = QFormLayout(box)
        self.broadcast_secs = QSpinBox()
        self.broadcast_secs.setRange(0, 86400)
        self.broadcast_secs.setSuffix(" s")
        self.broadcast_secs.setSpecialValueText("Firmware default")
        self.smart = QCheckBox("Smart broadcast (send sooner when moving)")
        self.gps_mode = QComboBox()
        self.fixed = QCheckBox("Use a fixed position (for a base station without GPS)")
        self.lat = QDoubleSpinBox()
        self.lat.setRange(-90, 90)
        self.lat.setDecimals(6)
        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(6)
        self.alt = QSpinBox()
        self.alt.setRange(-500, 10000)
        self.alt.setSuffix(" m")
        for w in (self.broadcast_secs, self.lat, self.lon, self.alt):
            w.valueChanged.connect(lambda _: self._mark_dirty("position"))
        for w in (self.smart, self.fixed):
            w.toggled.connect(lambda _: self._mark_dirty("position"))
        self.gps_mode.activated.connect(lambda _: self._mark_dirty("position"))
        self.fixed.toggled.connect(self._update_enabled)

        form.addRow("Broadcast every", self.broadcast_secs)
        form.addRow("", self.smart)
        form.addRow("GPS", self.gps_mode)
        form.addRow("", self.fixed)
        form.addRow("Latitude", self.lat)
        form.addRow("Longitude", self.lon)
        form.addRow("Altitude", self.alt)
        self.position_apply = QPushButton("Apply position settings…")
        self.position_apply.clicked.connect(self._apply_position)
        form.addRow("", self.position_apply)
        return box

    # ---- status → view ----

    def _mark_dirty(self, form):
        if not self._populating:
            self._dirty.add(form)

    def _history_group(self):
        from .charts import NodeCharts

        box = QGroupBox("This station's history")
        layout = QVBoxLayout(box)
        note = QLabel("Your radio reports these about once a minute. Channel utilization is how busy the mesh "
                      "is around you; transmit airtime is how much of it is this radio.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        self.history = NodeCharts(self.win.store, empty_text="Connect the radio to see this station's history.")
        self.history.setMinimumHeight(460)
        layout.addWidget(note)
        layout.addWidget(self.history)
        self.win.dataChanged.connect(self.history.refresh)
        return box

    def _status_changed(self, status):
        self.history.set_node(self.win.my_num)
        connected = status.get("connected")
        if status.get("hub_error"):
            self.banner.setText(f"{escape(status['hub_error'])}. Settings can't be read or changed until the "
                                "logger is running and connected to the radio.")
            self.banner.show()
        elif not connected:
            self.banner.setText("The logger is running but the radio isn't connected. It will reconnect "
                                "automatically (after a reboot this takes a few seconds).")
            self.banner.show()
        else:
            self.banner.hide()

        self._show_status(status if connected else {})
        if connected:
            snapshot = (status["node"], status["device"], status["position"])
            if snapshot != self._populated_from:
                self._populate_forms(status, only_clean=True)
                self._populated_from = snapshot
        self._update_enabled()

    def _show_status(self, s):
        node, metrics, lora = s.get("node", {}), s.get("metrics", {}), s.get("lora", {})

        def put(key, value):
            self.fields[key].setText("—" if value in (None, "") else str(value))

        put("name", f"{node.get('long_name')} ({node.get('short_name')})" if node else None)
        put("id", node.get("id"))
        put("hw", node.get("hw_model"))
        put("firmware", node.get("firmware"))
        put("licensed", ("Yes: unencrypted, callsign-identified" if node.get("is_licensed") else "No") if node else None)
        put("port", s.get("port"))
        put("role", (s.get("device") or {}).get("role"))
        put("region", lora.get("region"))
        put("preset", lora.get("modem_preset"))
        put("hops", lora.get("hop_limit"))
        put("tx", ("Yes" if lora.get("tx_enabled") else "No — receive only") if lora else None)
        put("battery", f"{metrics['batteryLevel']}%" if "batteryLevel" in metrics else None)
        put("voltage", f"{metrics['voltage']:.2f} V" if "voltage" in metrics else None)
        put("chutil", f"{metrics['channelUtilization']:.1f}%" if "channelUtilization" in metrics else None)
        put("airtx", f"{metrics['airUtilTx']:.1f}%" if "airUtilTx" in metrics else None)
        up = metrics.get("uptimeSeconds")
        put("uptime", f"{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m" if up is not None else None)
        put("channels", ", ".join(f"{c['index']}: {c['name']}" for c in s.get("channels", [])))

    def _populate_forms(self, status, only_clean):
        self._populating = True
        try:
            if not (only_clean and "owner" in self._dirty):
                self.long_name.setText(status["node"].get("long_name") or "")
                self.short_name.setText(status["node"].get("short_name") or "")
                self._dirty.discard("owner")
            if not (only_clean and "role" in self._dirty):
                self.role.clear()
                current = status["device"]["role"]
                roles = list(status.get("roles", []))
                if current not in roles:
                    roles.insert(0, current)  # e.g. a deprecated role set elsewhere
                self.role.addItems(roles)
                self.role.setCurrentText(current)
                self._dirty.discard("role")
            if not (only_clean and "position" in self._dirty):
                pos = status["position"]
                self.broadcast_secs.setValue(pos["broadcast_secs"])
                self.smart.setChecked(pos["smart_enabled"])
                self.gps_mode.clear()
                self.gps_mode.addItems(status.get("gps_modes", []))
                self.gps_mode.setCurrentText(pos["gps_mode"])
                self.fixed.setChecked(pos["fixed_position"])
                if pos.get("latitude") is not None:
                    self.lat.setValue(pos["latitude"])
                    self.lon.setValue(pos["longitude"])
                    self.alt.setValue(int(pos.get("altitude") or 0))
                self._dirty.discard("position")
        finally:
            self._populating = False

    def _reload_forms(self):
        self._dirty.clear()
        self._populated_from = None
        self.win.poll_status()

    def _update_enabled(self):
        ready = self.win.radio_ready
        for w in (self.reboot_button, self.announce_button, self.refresh_button, self.long_name, self.short_name,
                  self.owner_apply, self.role, self.role_apply, self.broadcast_secs, self.smart, self.gps_mode,
                  self.fixed, self.position_apply):
            w.setEnabled(ready)
        for w in (self.lat, self.lon, self.alt):
            w.setEnabled(ready and self.fixed.isChecked())

    # ---- commands ----

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.Cancel,
                                    QMessageBox.Cancel) == QMessageBox.Yes

    def _post(self, path, body, success, form=None):
        def done(result, error):
            if error:
                QMessageBox.warning(self, "Radio", f"That didn't work: {error}")
                return
            if form:
                self._dirty.discard(form)
                self._populated_from = None  # refill from the radio's new settings
            self.win.show_result(success, result)
            self.win.poll_status()

        self.win.hub.post(path, body, done)

    def _reboot(self):
        if self._confirm("Reboot radio", "Reboot the radio now?\n\nThe logger will lose the connection "
                                         "for a few seconds and reconnect on its own."):
            self._post("/api/reboot", {}, "Reboot requested")

    def _announce(self):
        self._post("/api/announce", {}, "Node info broadcast")

    def _apply_owner(self):
        long_name, short_name = self.long_name.text().strip(), self.short_name.text().strip()
        if not long_name or not short_name:
            QMessageBox.warning(self, "Owner", "Both a long and a short name are required.")
            return
        extra = ""
        if (self.win.status.get("node") or {}).get("is_licensed"):
            extra = ("\n\nLicensed mode stays on. Keep your callsign in the long name for station "
                     "identification.")
        if self._confirm("Change owner", f"Rename this node to “{long_name}” ({short_name})?{extra}"):
            self._post("/api/config/owner", {"long_name": long_name, "short_name": short_name},
                       "Owner updated", form="owner")

    def _apply_role(self):
        new, old = self.role.currentText(), (self.win.status.get("device") or {}).get("role")
        if new == old:
            self.win.toast(f"Role is already {new}")
            return
        warning = ""
        if new.startswith("ROUTER"):
            warning = ("\n\nRouter roles change how the whole mesh behaves. Check with the local "
                       "Meshtastic group before running one.")
        if self._confirm("Change role", f"Change role from {old} to {new}?\n\n{ROLE_HELP.get(new, '')}"
                                        f"{warning}\n\nThe radio will reboot to apply this."):
            self._post("/api/config/role", {"role": new}, f"Role set to {new}", form="role")

    def _apply_position(self):
        pos = (self.win.status.get("position") or {})
        body = {
            "broadcast_secs": self.broadcast_secs.value(),
            "smart_enabled": self.smart.isChecked(),
            "gps_mode": self.gps_mode.currentText(),
        }
        summary = [f"Broadcast interval: {self.broadcast_secs.text()}",
                   f"Smart broadcast: {'on' if self.smart.isChecked() else 'off'}",
                   f"GPS: {self.gps_mode.currentText()}"]
        if self.fixed.isChecked():
            if self.lat.value() == 0 and self.lon.value() == 0:
                QMessageBox.warning(self, "Position", "Enter a latitude and longitude for the fixed position.")
                return
            body["fixed"] = {"latitude": self.lat.value(), "longitude": self.lon.value(), "altitude": self.alt.value()}
            summary.append(f"Fixed position: {self.lat.value():.6f}, {self.lon.value():.6f}, {self.alt.value()} m")
        elif pos.get("fixed_position"):
            body["fixed"] = False
            summary.append("Fixed position: remove")
        if self._confirm("Position settings", "Apply these position settings?\n\n" + "\n".join(summary)):
            self._post("/api/config/position", body, "Position settings updated", form="position")
