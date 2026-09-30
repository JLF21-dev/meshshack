"""Device tab: which radios are plugged in, radio status, reboot/announce, and owner/role/position settings."""

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
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
        self.radios = RadiosGroup(win)
        grid.addWidget(self.radios, 0, 0, 1, 2)
        grid.addWidget(self._status_group(), 1, 0, 2, 1)
        grid.addWidget(self._actions_group(), 1, 1)
        grid.addWidget(self._owner_group(), 2, 1)
        grid.addWidget(self._role_group(), 3, 0)
        grid.addWidget(self._position_group(), 3, 1)
        grid.addWidget(self._history_group(), 4, 0, 1, 2)
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
                                "automatically (after a reboot this takes a few seconds). If it doesn't, "
                                "check Radios below: Scan, then link your Meshtastic radio.")
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


class RadiosGroup(QGroupBox):
    """The radios on USB, and which one is which. Linking is by hardware ID, so a linked radio is
    found again whichever USB socket it's moved to."""

    COLUMNS = ["Radio (hardware ID)", "USB port", "Linked as", "Node", "Answers as", "Device"]

    def __init__(self, win):
        super().__init__("Radios")
        self.win = win
        self.rows = []
        self.kinds = {"meshtastic": "Meshtastic", "meshcore": "MeshCore"}
        self._probes = {}  # hardware ID -> last probe, kept across refreshes
        self._seen = None  # (connected, hardware ID) the table was last refreshed for

        note = QLabel("Each radio is known by its hardware ID, which stays the same whichever USB socket it's "
                      "in. The logger uses the radio linked as Meshtastic. Scan asks each free radio what "
                      "firmware it runs; nothing is transmitted, though some boards restart when asked.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(150)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan)
        self.link_mt = QPushButton("Link as Meshtastic…")
        self.link_mt.clicked.connect(lambda: self._link("meshtastic"))
        self.link_mc = QPushButton("Link as MeshCore…")
        self.link_mc.clicked.connect(lambda: self._link("meshcore"))
        self.unlink_button = QPushButton("Unlink…")
        self.unlink_button.clicked.connect(self._unlink)
        buttons = QHBoxLayout()
        for b in (self.scan_button, self.link_mt, self.link_mc, self.unlink_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.message = QLabel()
        self.message.setWordWrap(True)

        self.meshcore_status = QLabel()
        self.meshcore_status.setWordWrap(True)
        self.meshcore_status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        layout.addWidget(self.meshcore_status)
        win.statusChanged.connect(self._status_changed)
        self._status_changed(win.status)

    def _status_changed(self, status):
        mc = status.get("meshcore") or {}
        key = (bool(status.get("connected")), status.get("hardware_id"), bool(status.get("hub_error")),
               mc.get("connected"), mc.get("hardware_id"))
        self._show_meshcore(mc, status.get("hub_error"))
        if key != self._seen:
            self._seen = key
            self.refresh()
        self._update_buttons()

    def refresh(self):
        self.win.hub.get("/api/radios", self._loaded)

    def _show_meshcore(self, mc, hub_error):
        if hub_error:
            self.meshcore_status.setText("")
        elif mc.get("connected"):
            r = mc.get("radio") or {}
            channels = ", ".join(c["name"] for c in mc.get("channels", [])) or "none"
            self.meshcore_status.setText(
                f"<b>MeshCore radio</b>: {escape(str(r.get('name')))} on {escape(str(mc.get('port')))} · "
                f"{escape(str(r.get('model')))}, {escape(str(r.get('firmware')))} · {r.get('freq_mhz')} MHz, "
                f"{r.get('bw_khz')} kHz, SF{r.get('sf')} · channels: {escape(channels)} · "
                "<i>receive only: MeshShack never transmits through it</i>")
        elif mc.get("linked"):
            self.meshcore_status.setText("<b>MeshCore radio</b>: linked but not connected; the logger keeps trying.")
        else:
            self.meshcore_status.setText("<b>MeshCore radio</b>: none linked. To log MeshCore too, scan, select "
                                         "the MeshCore radio and link it as MeshCore.")

    def scan(self):
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self.message.setText("Asking each free radio what it is (a few seconds each)…")
        self.win.hub.post("/api/radios/scan", {}, self._scanned)

    def _scanned(self, result, error):
        self.scan_button.setText("Scan")
        if result:
            for r in result["radios"]:
                if r.get("probe") and r["hardware_id"]:
                    self._probes[r["hardware_id"]] = r["probe"]
        self._loaded(result, error)
        if not error:
            self.message.setText(self._advice())

    def _loaded(self, result, error):
        if error:
            self.rows = []
            self.message.setText(f"Can't list radios: {error}")
        else:
            self.rows = result["radios"]
            self.kinds = result.get("kinds") or self.kinds
            if not self.message.text().startswith(("Linked", "Unlinked")):
                self.message.setText("")
        self._fill()
        self._update_buttons()

    def _answers_as(self, r):
        if r.get("connected_as"):
            return f"{self.kinds[r['connected_as']]} (connected now)"
        if r.get("connected"):
            return "Meshtastic (connected now)"
        if r.get("in_use"):
            return "In use by another program"
        probe = self._probes.get(r["hardware_id"])
        if not r["present"] or probe is None:
            return ""
        if probe.get("kind"):
            return f"{self.kinds[probe['kind']]} {probe.get('node') or ''}".strip()
        return f"Unknown: {probe.get('error')}"

    def _fill(self):
        selected = self._selected()
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            link = r.get("link") or {}
            values = [r["hardware_id"] or "(none reported)", r["port"] or "Not plugged in",
                      self.kinds.get(link.get("kind"), ""), link.get("node") or "", self._answers_as(r),
                      r.get("description") or ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if not r["present"]:
                    item.setForeground(Qt.gray)
                self.table.setItem(i, col, item)
            if selected and r["hardware_id"] == selected["hardware_id"]:
                self.table.selectRow(i)

    def _advice(self):
        found = [(r, self._probes.get(r["hardware_id"])) for r in self.rows if r["present"]]
        unlinked = [(r, p) for r, p in found if not r.get("link") and p and p.get("kind")]
        if unlinked:
            names = ", ".join(f"{r['port']} ({self.kinds[p['kind']]})" for r, p in unlinked)
            return f"Not linked yet: {names}. Select one and link it as what it answered."
        return "Scan finished."

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.rows[rows[0].row()] if rows and rows[0].row() < len(self.rows) else None

    def _update_buttons(self):
        hub_ok = not self.win.status.get("hub_error")
        r = self._selected()
        self.scan_button.setEnabled(hub_ok and self.scan_button.text() == "Scan")
        can_link = hub_ok and r is not None and bool(r["hardware_id"])
        kind = (r or {}).get("link", None) and r["link"].get("kind")
        self.link_mt.setEnabled(can_link and kind != "meshtastic")
        self.link_mc.setEnabled(can_link and kind != "meshcore" and not (r or {}).get("connected"))
        self.unlink_button.setEnabled(hub_ok and r is not None and bool(r.get("link")))

    def _link(self, kind):
        r = self._selected()
        if r is None:
            return
        probe = self._probes.get(r["hardware_id"]) or {}
        if probe.get("kind") and probe["kind"] != kind:
            mismatch = (f"\n\nThis radio answered the last scan as {self.kinds[probe['kind']]}, "
                        f"not {self.kinds[kind]}.")
        elif not probe.get("kind") and not r.get("connected"):
            mismatch = "\n\nIt hasn't been scanned, so what it runs isn't confirmed. Consider Scan first."
        else:
            mismatch = ""
        if kind == "meshtastic":
            effect = ("The logger will switch to this radio within a few seconds, and everything MeshShack sends "
                      "will go out through it. Any other radio linked as Meshtastic is unlinked.")
        else:
            effect = ("MeshShack doesn't log MeshCore traffic yet; linking reserves this radio so the "
                      "Meshtastic logger always leaves it alone.")
        where = r["port"] or "not plugged in"
        if QMessageBox.question(self, "Link radio", f"Link {r['hardware_id']} ({where}) as the "
                                f"{self.kinds[kind]} radio?\n\n{effect}{mismatch}",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        body = {"hardware_id": r["hardware_id"], "kind": kind}
        if probe.get("kind") == kind:
            body["node"] = probe.get("node")
            body["label"] = (probe.get("detail") or {}).get("name")
        elif r.get("connected"):
            node = self.win.status.get("node") or {}
            body["node"], body["label"] = node.get("id"), node.get("long_name")

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Link radio", f"That didn't work: {error}")
                return
            self.message.setText(f"Linked {r['hardware_id']} as the {self.kinds[kind]} radio.")
            self.refresh()
            self.win.poll_status()

        self.win.hub.post("/api/radios/link", body, done)

    def _unlink(self):
        r = self._selected()
        if r is None or not r.get("link"):
            return
        extra = ""
        if r["link"].get("kind") == "meshtastic":
            extra = ("\n\nThe logger stays connected for now. After it next restarts, it only picks a radio "
                     "on its own when exactly one possible Meshtastic radio is plugged in.")
        if QMessageBox.question(self, "Unlink radio", f"Unlink {r['hardware_id']}?{extra}",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Unlink radio", f"That didn't work: {error}")
                return
            self.message.setText(f"Unlinked {r['hardware_id']}.")
            self.refresh()

        self.win.hub.post("/api/radios/unlink", {"hardware_id": r["hardware_id"]}, done)
