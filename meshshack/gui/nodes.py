"""Nodes tab: sortable table of every node heard, per-node actions, and a detail panel
(details, history charts, and the results of traceroutes and requests)."""

import time
from pathlib import Path
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMenu, QMessageBox, QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget,
)

from ..export import EXPORTS
from .charts import NodeCharts
from .common import (
    WINDOWS, cell_size_text, distance_km, effective_precision, fmt_ago, since_for, station_position,
)

COLUMNS = ["Short", "Long name", "ID", "Hardware", "Role", "Last heard", "Via", "Direct SNR", "Direct RSSI", "Hops",
           "Battery", "Distance"]
HEADER_TIPS = {
    "Last heard": "Last packet from this node by any path, including the internet (MQTT)",
    "Via": "How this node's packets reach you: Direct, Radio (with the fewest hops), Internet (flagged MQTT), "
           "Internet? (not flagged, but farther away than radio carries in its hop count), both, or Unknown (older "
           "firmware without hop information). Hover a cell for the reason.",
    "Direct SNR": "Signal from the last packet heard straight from this node (0 hops). Blank if you've only "
                  "heard it through relays: a relayed packet's signal belongs to the last relay, not the node.",
    "Direct RSSI": "Signal strength of the last packet heard straight from this node (0 hops)",
    "Hops": "Hops taken by the last packet heard over the radio (MQTT packets don't count)",
    "Distance": "≈ means at least one of the two positions is rounded (see the map's position areas)",
}
FAVORITE_TIP = ("Favorites are stored on your radio. With the CLIENT_BASE role, your radio gives their traffic "
                "router priority. Nothing is transmitted.")
ACTIONS = [
    ("Message", "dm"),
    ("Traceroute", "traceroute"),
    ("Request position", "position"),
    ("Request telemetry", "telemetry"),
    ("Request node info", "nodeinfo"),
]


class SortState:
    """Shared by a table's items: whether favorites stay on top, and the current sort order."""

    favorites_first = True
    order = Qt.DescendingOrder


class SortItem(QTableWidgetItem):
    """Shows display text but sorts by a separate key (numbers, timestamps). Blanks sort last and,
    with favorites first, favorites sort above everything else, in either direction."""

    def __init__(self, text, key=None, favorite=False, state=None):
        super().__init__(text)
        self.key = key
        self.favorite = favorite
        self.state = state

    def __lt__(self, other):
        # Qt reverses the comparison for a descending sort, so anything meant to stay on top (or at
        # the bottom) regardless of direction is flipped when descending.
        ascending = self.state is None or self.state.order == Qt.AscendingOrder
        if self.state is not None and self.state.favorites_first and self.favorite != getattr(other, "favorite", False):
            return self.favorite if ascending else not self.favorite
        a, b = self.key, getattr(other, "key", None)
        if a is None or b is None:
            if (a is None) == (b is None):
                return False
            blank_last = a is not None  # self has a value and other is blank: self goes first
            return blank_last if ascending else not blank_last
        return a < b


class NodesTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store

        self.window_box = QComboBox()
        for label, seconds in WINDOWS:
            self.window_box.addItem(label, seconds)
        self.window_box.setCurrentIndex(int(win.settings.value("nodes/window", 1)))
        self.window_box.currentIndexChanged.connect(self._window_changed)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, ID or hardware")
        self.search.textChanged.connect(self.refresh)
        self.count = QLabel()

        top = QHBoxLayout()
        top.addWidget(QLabel("Heard:"))
        top.addWidget(self.window_box)
        top.addWidget(self.search, 1)
        self.radio_only = QCheckBox("Hide internet-only")
        self.radio_only.setToolTip("Hide nodes heard only through the internet (flagged MQTT, or too far for their hops)")
        self.radio_only.setChecked(win.settings.value("nodes/radio_only", "false") == "true")
        self.radio_only.toggled.connect(lambda on: (win.settings.setValue("nodes/radio_only", "true" if on else "false"),
                                                    self.refresh()))
        top.addWidget(self.radio_only)
        self.favorites_first = QCheckBox("★ Favorites first")
        self.favorites_first.setChecked(win.settings.value("nodes/favorites_first", "true") == "true")
        self.favorites_first.toggled.connect(self._favorites_first_changed)
        top.addWidget(self.favorites_first)
        export = QToolButton()
        export.setText("Export")
        export.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(export)
        for (what, fmt), (_, _, label) in EXPORTS.items():
            export_menu.addAction(label, lambda w=what, f=fmt: self._export(w, f))
        export.setMenu(export_menu)
        export.setToolTip("Save to a file, covering the time range picked under Heard")
        top.addWidget(export)
        top.addWidget(self.count)

        self.sort_state = SortState()
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        for col, name in enumerate(COLUMNS):
            if name in HEADER_TIPS:
                self.table.horizontalHeaderItem(col).setToolTip(HEADER_TIPS[name])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COLUMNS.index("Last heard"), Qt.DescendingOrder)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._act("dm"))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        self.buttons = {}
        button_row = QHBoxLayout()
        for label, action in ACTIONS:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, a=action: self._act(a))
            self.buttons[action] = button
            button_row.addWidget(button)
        self.favorite_button = QPushButton("★ Favorite")
        self.favorite_button.setToolTip(FAVORITE_TIP)
        self.favorite_button.clicked.connect(self._toggle_favorite)
        self.ignore_button = QPushButton("Ignore…")
        self.ignore_button.clicked.connect(self._toggle_ignored)
        button_row.addWidget(self.favorite_button)
        button_row.addWidget(self.ignore_button)
        button_row.addStretch(1)

        table_box = QWidget()
        table_layout = QVBoxLayout(table_box)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addLayout(top)
        table_layout.addWidget(self.table, 1)
        table_layout.addLayout(button_row)

        self.activity = QListWidget()
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.charts = NodeCharts(self.store)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self.details, "Details")
        self.detail_tabs.addTab(self.charts, "Charts")
        self.detail_tabs.addTab(self.activity, "Activity")
        self.detail_tabs.setCurrentIndex(int(win.settings.value("nodes/detail_tab", 0)))
        self.detail_tabs.currentChanged.connect(lambda i: win.settings.setValue("nodes/detail_tab", i))

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(table_box)
        splitter.addWidget(self.detail_tabs)
        splitter.setSizes([420, 320])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        win.dataChanged.connect(self.refresh)
        win.statusChanged.connect(lambda _: self.refresh())
        win.activity.updated.connect(self._show_activity)
        self._show_activity()

    def _window_changed(self, index):
        self.win.settings.setValue("nodes/window", index)
        self.refresh()

    def _selected_num(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole)

    def refresh(self):
        selected = self._selected_num()
        since = since_for(self.window_box.currentData())
        needle = self.search.text().strip().lower()
        my_num = self.win.my_num
        my_lat, my_lon, my_bits = station_position(self.win.status, self.store, my_num)
        reported_bits = self.store.position_precision()
        paths = self.win.paths()

        nodes = [n for n in self.store.nodes(since=since) if n["num"] != my_num]
        if self.radio_only.isChecked():
            nodes = [n for n in nodes if (paths.get(n["num"]) or {}).get("kind") not in ("mqtt", "inferred")]
        if needle:
            nodes = [n for n in nodes
                     if needle in " ".join(str(n[k] or "") for k in ("short_name", "long_name", "node_id", "hw_model")).lower()]

        sort_column = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self.sort_state.favorites_first = self.favorites_first.isChecked()
        self.sort_state.order = sort_order
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(nodes))
        for row, n in enumerate(nodes):
            dist = distance_km(my_lat, my_lon, n["latitude"], n["longitude"])
            p = paths.get(n["num"]) or {"kind": "unknown", "label": "", "why": "Nothing from it logged yet"}
            via = p["kind"]
            hops = None if via in ("mqtt", "inferred") else n["hops_away"]  # meaningless across the internet
            # ≈ when either end is a rounded position (see common.effective_precision).
            approx = my_bits or effective_precision(n["latitude"], n["longitude"], reported_bits.get(n["num"]))
            favorite = bool(n["is_favorite"])
            cells = [
                SortItem(("★ " if favorite else "") + (n["short_name"] or ""), (n["short_name"] or "").lower() or None),
                SortItem(n["long_name"] or "", (n["long_name"] or "").lower() or None),
                SortItem(n["node_id"], n["node_id"]),
                SortItem(n["hw_model"] or "", n["hw_model"]),
                SortItem(n["role"] or "", n["role"]),
                SortItem(fmt_ago(n["last_heard"]), n["last_heard"]),
                SortItem(p["label"], via if p["label"] else None),
                SortItem(f"{n['last_snr']:.1f}" if n["last_snr"] is not None else "", n["last_snr"]),
                SortItem(str(n["last_rssi"]) if n["last_rssi"] is not None else "", n["last_rssi"]),
                SortItem(str(hops) if hops is not None else "", hops),
                SortItem(f"{n['battery_level']}%" if n["battery_level"] is not None else "", n["battery_level"]),
                SortItem(f"{'≈ ' if approx else ''}{dist:.1f} km" if dist is not None else "", dist),
            ]
            cells[0].setData(Qt.UserRole, n["num"])
            cells[COLUMNS.index("Via")].setToolTip(p["why"])
            for item in cells:
                item.favorite, item.state = favorite, self.sort_state
                if n["is_ignored"]:
                    item.setForeground(Qt.gray)
            if n["is_ignored"]:
                cells[0].setToolTip("Ignored: your radio drops this node's packets")
            if n["direct_heard"]:
                for name in ("Direct SNR", "Direct RSSI"):
                    cells[COLUMNS.index(name)].setToolTip(f"Heard directly {fmt_ago(n['direct_heard'])}")
            for col, item in enumerate(cells):
                if col >= COLUMNS.index("Last heard") and COLUMNS[col] != "Via":
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(sort_column, sort_order)

        if selected is not None:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.UserRole) == selected:
                    self.table.selectRow(row)
                    break
        if not getattr(self, "_sized", False) and nodes:
            self.table.resizeColumnsToContents()
            self._sized = True

        self.count.setText(f"{len(nodes)} node{'s' if len(nodes) != 1 else ''}")
        self._update_buttons()
        current = self._selected_num()
        if current != selected or current is not None:
            self._show_details(current)
            self.charts.set_node(current)
            self.charts.refresh()

    def _update_buttons(self):
        num = self._selected_num()
        has_node = num is not None
        for action, button in self.buttons.items():
            button.setEnabled(has_node and (action == "dm" or self.win.radio_ready))
        row = self.store.node(num) if has_node else None
        for button in (self.favorite_button, self.ignore_button):
            button.setEnabled(has_node and self.win.radio_ready)
        self.favorite_button.setText("☆ Unfavorite" if row is not None and row["is_favorite"] else "★ Favorite")
        self.ignore_button.setText("Stop ignoring" if row is not None and row["is_ignored"] else "Ignore…")

    def _selection_changed(self):
        self._update_buttons()
        num = self._selected_num()
        self.charts.set_node(num)
        self._show_details(num)

    def _export(self, what, fmt):
        exporter, ext, label = EXPORTS[(what, fmt)]
        stamp = time.strftime("%Y%m%d-%H%M")
        default = str(Path.home() / f"meshshack-{what}-{stamp}.{ext}")
        path, _ = QFileDialog.getSaveFileName(self, f"Export {label}", default, f"{ext.upper()} files (*.{ext})")
        if not path:
            return
        since = since_for(self.window_box.currentData())
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                count = exporter(self.store, f, since=since)
        except OSError as ex:
            QMessageBox.warning(self, "Export", f"Couldn't write {path}: {ex}")
            return
        self.win.toast(f"Exported {count} record{'s' if count != 1 else ''} to {path}")

    def _favorites_first_changed(self, on):
        self.win.settings.setValue("nodes/favorites_first", "true" if on else "false")
        self.refresh()

    def _toggle_favorite(self):
        num = self._selected_num()
        row = self.store.node(num) if num is not None else None
        if row is not None:
            self._set_flag("/api/nodes/favorite", {"node": num, "favorite": not row["is_favorite"]},
                           "Removed from favorites" if row["is_favorite"] else "Added to favorites")

    def _toggle_ignored(self):
        num = self._selected_num()
        row = self.store.node(num) if num is not None else None
        if row is None:
            return
        if not row["is_ignored"] and QMessageBox.question(
            self, "Ignore node",
            f"Ignore {row['short_name'] or row['node_id']}?\n\nYour radio will drop everything from this node, "
            "so it also won't be logged. Nothing is transmitted, and you can undo it here.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self._set_flag("/api/nodes/ignore", {"node": num, "ignored": not row["is_ignored"]},
                       "No longer ignored" if row["is_ignored"] else "Ignored")

    def _set_flag(self, path, body, success):
        def done(result, error):
            if error:
                self.win.toast(f"That didn't work: {error}", 10000)
            else:
                self.win.toast(success)
            self.refresh()

        self.win.hub.post(path, body, done)

    def _show_details(self, num):
        if num is None:
            self.details.setHtml("<p style='color:gray'>Select a node to see its details.</p>")
            return
        n = self.store.node(num)
        if n is None:
            return
        summary = self.store.node_summary(num)
        p = self.win.paths().get(num) or {"kind": "unknown", "label": "Not logged yet", "why": "Nothing from it logged yet",
                                         "direct": 0, "radio": 0, "mqtt": 0, "inferred": 0, "unknown": 0}
        via = p["kind"]
        my_lat, my_lon, my_bits = station_position(self.win.status, self.store, self.win.my_num)
        bits = effective_precision(n["latitude"], n["longitude"], self.store.position_precision().get(num))

        def when(ts):
            return f"{fmt_ago(ts)} ({time.strftime('%b %-d %H:%M', time.localtime(ts))})" if ts else "never"

        rows = [
            ("Node", f"{n['long_name'] or ''} ({n['short_name'] or '?'}) · {n['node_id']}"),
            ("Hardware / role", f"{n['hw_model'] or '?'} · {n['role'] or '?'}"),
            ("Heard", f"{p['label']}. {p['why']}"),
            ("Packets by path", f"{p['direct']} direct, {p['radio']} relayed over radio, {p['mqtt']} internet (flagged), "
                                f"{p['inferred']} internet (inferred), {p['unknown']} unknown"),
            ("First seen", when(n["first_seen"])),
            ("Last heard", f"{when(n['last_heard'])}; over the radio {when(n['rf_heard'])}; "
                           f"directly {when(n['direct_heard'])}"),
        ]
        if n["last_snr"] is not None:
            rows.append(("Direct signal", f"SNR {n['last_snr']:.1f} dB, RSSI {n['last_rssi']} "
                                          f"(heard directly {fmt_ago(n['direct_heard'])})"))
        if via not in ("mqtt", "inferred") and n["hops_away"] is not None:
            rows.append(("Hops away", str(n["hops_away"])))
        if n["battery_level"] is not None:
            battery = "external power" if n["battery_level"] > 100 else f"{n['battery_level']}%"
            rows.append(("Battery", battery + (f", {n['voltage']:.2f} V" if n["voltage"] is not None else "")))
        if n["latitude"] is not None:
            where = f"{n['latitude']:.5f}, {n['longitude']:.5f}"
            where += f" (rounded: somewhere in a {cell_size_text(n['latitude'], bits)} area)" if bits else " (exact)"
            dist = distance_km(my_lat, my_lon, n["latitude"], n["longitude"])
            if dist is not None:
                where += f"; {'≈ ' if bits or my_bits else ''}{dist:.1f} km from you"
            rows.append(("Position", where))
        flags = [text for flag, text in (("is_favorite", "★ Favorite"), ("is_ignored", "Ignored: your radio drops "
                 "its packets")) if n[flag]]
        if flags:
            rows.append(("On your radio", " · ".join(flags)))
        if summary["by_type"]:
            rows.append(("Packets logged", ", ".join(f"{r['portnum'].removesuffix('_APP').lower()} {r['c']}"
                                                     for r in summary["by_type"])))
        rows.append(("Messages", f"{summary['messages']} to or from this node"))
        trace = summary["last_traceroute"]
        if trace is not None:
            text = next((line for ts, line in self.win.activity.entries
                         if line.startswith("Traceroute to") and ts >= trace["sent_at"]), None)
            rows.append(("Last traceroute", f"{fmt_ago(trace['sent_at'])}: {text or trace['status']}"))
        html = "".join(f"<tr><td style='color:gray; padding-right:12px; white-space:nowrap'>{escape(k)}</td>"
                       f"<td>{escape(v)}</td></tr>" for k, v in rows)
        self.details.setHtml(f"<table cellspacing='0' cellpadding='3'>{html}</table>")

    def _context_menu(self, pos):
        if self._selected_num() is None:
            return
        menu = QMenu(self)
        for label, action in ACTIONS:
            item = menu.addAction(label)
            item.setEnabled(self.buttons[action].isEnabled())
            item.triggered.connect(lambda _=False, a=action: self._act(a))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _act(self, action):
        num = self._selected_num()
        if num is None:
            return
        if action == "dm":
            self.win.open_dm(num)
        else:
            self.win.send_request(num, action)

    def _show_activity(self):
        self.activity.clear()
        lines = self.win.activity.lines()
        self.activity.addItems(lines or ["Nothing yet. Select a node and run a traceroute or request."])
