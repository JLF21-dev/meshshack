"""Automation tab: scheduled messages you define, with content from data sources (../automation.py).

Everything goes through the logger's API (app-only endpoints): the logger runs the jobs and
sends through the airtime gatekeeper. New jobs start as dry runs; going live asks first.
"""

import time
from html import escape

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTimeEdit, QToolButton, QVBoxLayout, QWidget,
)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PLACEHOLDER_HELP = (
    "Placeholders: {time} {date} {time_utc} {date_utc} · {clock.offset_ms:+.0f} (NIST clock check) · "
    "{station.channel_util:.0f} {station.nodes_heard_24h} {station.nodes_heard_7d} {station.packets_24h} "
    "{station.direct_neighbors_24h} · {node.SHORT.battery} (also voltage, snr, hops, name, last_heard) · "
    "{weather.name} {weather.short} {weather.temp} {weather.temp_unit} {weather.wind} {weather.wind_dir} "
    "(US National Weather Service, for this station's area) · {yoursource.field} from the data sources "
    "below · {cmd.name} for a local command. Add a format after a colon, e.g. {weather.temp:.0f}. "
    "Use {{ and }} for literal braces.")


def destination_text(dest, win):
    if "to" in dest:
        row = win.store.node(dest["to"])
        name = (row["short_name"] if row is not None and row["short_name"] else None) or f"!{dest['to']:08x}"
        return f"@ {name} (direct message)"
    channels = {c["index"]: c["name"] for c in win.status.get("channels", [])}
    return f"# {channels.get(dest.get('channel', 0), 'channel ' + str(dest.get('channel', 0)))} (broadcast)"


class JobDialog(QDialog):
    """Create or edit a job; Preview renders it with live data through the logger."""

    def __init__(self, win, job=None):
        super().__init__(win)
        self.win = win
        self.job = job or {}
        self.setWindowTitle("Edit automation job" if job and job.get("id") else "New automation job")
        self.resize(760, 640)

        self.name = QLineEdit(self.job.get("name", ""))
        trigger = self.job.get("trigger") or {"type": "daily", "at": "07:00"}
        self.kind = QComboBox()
        for label, kind in (("Daily", "daily"), ("Weekly", "weekly"), ("Every N hours", "every")):
            self.kind.addItem(label, kind)
        self.kind.setCurrentIndex(max(0, self.kind.findData(trigger["type"])))
        self.at = QTimeEdit(QTime.fromString(trigger.get("at", "07:00"), "HH:mm"))
        self.at.setDisplayFormat("HH:mm")
        self.weekday = QComboBox()
        self.weekday.addItems(WEEKDAYS)
        self.weekday.setCurrentIndex(trigger.get("weekday", 6))
        self.hours = QSpinBox()
        self.hours.setRange(6, 24 * 14)
        self.hours.setSuffix(" hours")
        self.hours.setValue(int(trigger.get("hours", 24)))
        self.kind.currentIndexChanged.connect(self._schedule_fields)
        schedule = QHBoxLayout()
        for w in (self.kind, self.weekday, self.at, self.hours):
            schedule.addWidget(w)
        schedule.addStretch(1)

        dest = self.job.get("destination") or {"channel": 0}
        self.dest = QComboBox()
        for ch in win.status.get("channels", []) or [{"index": 0, "name": "Primary"}]:
            self.dest.addItem(f"# {ch['name']} (broadcast on channel {ch['index']})", ("channel", ch["index"]))
        self.dest.addItem("Direct message to a node…", ("to", None))
        self.dm_node = QLineEdit()
        self.dm_node.setPlaceholderText("short name or !id")
        if "to" in dest:
            self.dest.setCurrentIndex(self.dest.count() - 1)
            row = win.store.node(dest["to"])
            self.dm_node.setText(row["short_name"] if row is not None and row["short_name"] else f"!{dest['to']:08x}")
        else:
            self.dest.setCurrentIndex(max(0, self.dest.findData(("channel", dest.get("channel", 0)))))
        self.dest.currentIndexChanged.connect(lambda _: self.dm_node.setVisible(self.dest.currentData()[0] == "to"))
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest, 1)
        dest_row.addWidget(self.dm_node)

        self.template = QPlainTextEdit(self.job.get("template", ""))
        self.template.setPlaceholderText("e.g. Good morning mesh! {weather.name}: {weather.short}, {weather.temp}°F.")
        self.template.setMaximumHeight(90)
        help_label = QLabel(PLACEHOLDER_HELP)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: small")
        help_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.sources = QTableWidget(0, 4)
        self.sources.setHorizontalHeaderLabels(["Type", "Name", "URL (http) or command", "Fields: key=path; key=path"])
        self.sources.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.sources.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.sources.verticalHeader().setVisible(False)
        for s in self.job.get("sources") or []:
            self._add_source(s)
        add_http = QPushButton("Add HTTP/JSON source")
        add_http.clicked.connect(lambda: self._add_source({"type": "http"}))
        add_cmd = QPushButton("Add command source")
        add_cmd.clicked.connect(lambda: self._add_source({"type": "command"}))
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.sources.removeRow(self.sources.currentRow()))
        source_buttons = QHBoxLayout()
        for b in (add_http, add_cmd, remove):
            source_buttons.addWidget(b)
        source_buttons.addStretch(1)

        self.preview = QLabel("Preview fills the template with live data, without sending anything.")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        preview_button = QPushButton("Preview")
        preview_button.clicked.connect(self._preview)
        preview_row = QHBoxLayout()
        preview_row.addWidget(preview_button, 0, Qt.AlignTop)
        preview_row.addWidget(self.preview, 1)

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Schedule", schedule)
        form.addRow("Sends to", dest_row)
        form.addRow("Message", self.template)
        form.addRow("", help_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        note = QLabel("Saved jobs run in the logger. A new job is a dry run (it records what it would send) "
                      "until you choose Go live.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("<b>Data sources</b> (optional): an HTTP source's fields become "
                                "{name.key}; a key's path picks from the JSON, e.g. current.temp or list.0.name"))
        layout.addWidget(self.sources)
        layout.addLayout(source_buttons)
        layout.addLayout(preview_row)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self._schedule_fields()
        self.dm_node.setVisible(self.dest.currentData()[0] == "to")
        self.result = None

    def _schedule_fields(self):
        kind = self.kind.currentData()
        self.at.setVisible(kind != "every")
        self.weekday.setVisible(kind == "weekly")
        self.hours.setVisible(kind == "every")

    def _add_source(self, s):
        r = self.sources.rowCount()
        self.sources.insertRow(r)
        kind = QComboBox()
        kind.addItem("HTTP/JSON", "http")
        kind.addItem("Command", "command")
        kind.setCurrentIndex(0 if s.get("type", "http") == "http" else 1)
        self.sources.setCellWidget(r, 0, kind)
        fields = "; ".join(f"{k}={v}" for k, v in (s.get("fields") or {}).items())
        for c, text in ((1, s.get("name", "")), (2, s.get("url") or s.get("command") or ""), (3, fields)):
            self.sources.setItem(r, c, QTableWidgetItem(text))

    def _job(self):
        kind = self.kind.currentData()
        trigger = {"type": kind}
        if kind in ("daily", "weekly"):
            trigger["at"] = self.at.time().toString("HH:mm")
        if kind == "weekly":
            trigger["weekday"] = self.weekday.currentIndex()
        if kind == "every":
            trigger["hours"] = self.hours.value()
        what, index = self.dest.currentData()
        if what == "channel":
            dest = {"channel": index}
        else:
            name = self.dm_node.text().strip()
            rows = self.win.store._query("SELECT num FROM nodes WHERE short_name = ? OR node_id = ?", (name, name))
            if not rows:
                raise ValueError(f"no known node called {name!r}")
            dest = {"to": rows[0]["num"]}
        sources = []
        for r in range(self.sources.rowCount()):
            text = lambda c: (self.sources.item(r, c).text().strip() if self.sources.item(r, c) else "")  # noqa: E731
            s = {"type": self.sources.cellWidget(r, 0).currentData(), "name": text(1)}
            if s["type"] == "http":
                s["url"] = text(2)
                s["fields"] = dict(p.split("=", 1) for p in (x.strip() for x in text(3).split(";")) if "=" in p)
                s["fields"] = {k.strip(): v.strip() for k, v in s["fields"].items()}
            else:
                s["command"] = text(2)
            sources.append(s)
        job = {"id": self.job.get("id"), "name": self.name.text().strip(), "trigger": trigger, "destination": dest,
               "template": self.template.toPlainText(), "sources": sources}
        if self.job.get("id") is not None:
            job["enabled"], job["dry_run"] = self.job.get("enabled", True), self.job.get("dry_run", True)
        return job

    def _preview(self):
        try:
            job = self._job()
        except ValueError as ex:
            self.preview.setText(f"<span style='color:#c5221f'>{escape(str(ex))}</span>")
            return
        self.preview.setText("Filling in…")

        def done(result, error):
            if error:
                self.preview.setText(f"<span style='color:#c5221f'>{escape(str(error))}</span>")
            elif result["problem"]:
                self.preview.setText(f"<span style='color:#c5221f'>Wouldn't send: {escape(result['problem'])}</span>")
            else:
                self.preview.setText(f"“{escape(result['text'])}”<br><span style='color:gray'>{result['bytes']} of "
                                     f"{result['limit']} bytes</span>")

        self.win.hub.post("/api/automation/preview", job, done)

    def _save(self):
        try:
            job = self._job()
        except ValueError as ex:
            QMessageBox.warning(self, "Automation", str(ex))
            return

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Automation", f"Not saved: {error}")
            else:
                self.result = result
                self.accept()

        self.win.hub.post("/api/automation/save", job, done)


class AutomationTab(QWidget):
    JOB_COLUMNS = ["Job", "Schedule", "Sends to", "Mode", "Next run", "Last result"]
    RUN_COLUMNS = ["Time", "Job", "Result", "Message, or why not"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.state = {"jobs": [], "runs": [], "presets": {}, "allow_commands": False}

        intro = QLabel(
            "Your own messages, sent on a schedule, with content from data sources. This is the one part of "
            "MeshShack that transmits by itself, so each job sends at most every 6 hours, at most 4 automated "
            "sends go out a day in total, 30 s apart, never while the channel is busy (over 20%) or transmitting "
            "is switched off. A message that's too long, or whose data can't be fetched, is skipped, never "
            "truncated. New jobs are dry runs until you choose Go live. Nothing replies to incoming messages.")
        intro.setWordWrap(True)
        self.jobs = self._table(self.JOB_COLUMNS)
        self.jobs.itemSelectionChanged.connect(self._update_buttons)
        self.jobs.doubleClicked.connect(lambda _: self._edit())
        self.runs = self._table(self.RUN_COLUMNS)

        self.buttons = {}
        row = QHBoxLayout()
        new = QPushButton("New job…")
        new.clicked.connect(lambda: self._open_editor(None))
        self.preset_button = QToolButton()
        self.preset_button.setText("From preset")
        self.preset_button.setPopupMode(QToolButton.InstantPopup)
        row.addWidget(new)
        row.addWidget(self.preset_button)
        for key, label, handler in (("edit", "Edit…", self._edit), ("preview", "Preview now", self._preview),
                                    ("toggle", "Turn off", self._toggle), ("live", "Go live…", self._live),
                                    ("delete", "Delete…", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(handler)
            self.buttons[key] = b
            row.addWidget(b)
        row.addStretch(1)
        self.allow_commands = QCheckBox("Allow local commands as data sources")
        self.allow_commands.setToolTip("Lets jobs run a command on this computer (as the logger's user) and use "
                                       "the first line of its output. Off unless you need it.")
        self.allow_commands.clicked.connect(self._toggle_commands)
        row.addWidget(self.allow_commands)
        self.message = QLabel()
        self.message.setStyleSheet("color: gray")

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(intro)
        top_layout.addWidget(self.jobs, 1)
        top_layout.addLayout(row)
        top_layout.addWidget(self.message)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel("<b>Runs</b>: every scheduled run, including dry runs and skipped ones"))
        bottom_layout.addWidget(self.runs, 1)
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([380, 260])
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        win.dataChanged.connect(self._maybe_reload)
        self._loaded_at = 0
        self._update_buttons()

    @staticmethod
    def _table(columns):
        t = QTableWidget(0, len(columns))
        t.setHorizontalHeaderLabels(columns)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    # ---- loading ----

    def showEvent(self, event):
        super().showEvent(event)
        self.reload()

    def _maybe_reload(self):
        if self.isVisible() and time.time() - self._loaded_at > 30:
            self.reload()

    def reload(self):
        self._loaded_at = time.time()

        def done(result, error):
            if error:
                self.message.setText(f"Can't reach the logger: {error}")
                return
            self.state = result
            self.message.setText("")
            self._fill()

        self.win.hub.get("/api/automation", done)

    def _fill(self):
        selected = self._selected()
        jobs = self.state["jobs"]
        last = {}
        for run in reversed(self.state["runs"]):
            last[run["job_id"]] = run
        self.jobs.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            mode = "Off" if not job["enabled"] else "Dry run" if job["dry_run"] else "LIVE"
            nxt = time.strftime("%a %b %-d %H:%M", time.localtime(job["next_run"])) if job["next_run"] else "—"
            run = last.get(job["id"])
            result = f"{run['status']} {time.strftime('%b %-d %H:%M', time.localtime(run['at']))}" if run else "not yet"
            for c, text in enumerate((job["name"], job["schedule"], destination_text(job["destination"], self.win),
                                      mode, nxt, result)):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, job["id"])
                if c == 3 and mode == "LIVE":
                    item.setForeground(Qt.red)
                self.jobs.setItem(r, c, item)
            if selected is not None and selected["id"] == job["id"]:
                self.jobs.selectRow(r)
        runs = self.state["runs"]
        self.runs.setRowCount(len(runs))
        for r, run in enumerate(runs):
            detail = run["text"] or ""
            if run["detail"] and run["status"] != "dry run":
                detail = f"{run['detail']}" + (f" — {run['text']}" if run["text"] else "")
            for c, text in enumerate((time.strftime("%b %-d %H:%M", time.localtime(run["at"])), run["job_name"] or "",
                                      run["status"], detail)):
                self.runs.setItem(r, c, QTableWidgetItem(text))
        menu = QMenu(self.preset_button)
        for name in self.state["presets"]:
            menu.addAction(name, lambda n=name: self._open_editor({**self.state["presets"][n], "name": n,
                                                                    "destination": {"channel": 0}}))
        self.preset_button.setMenu(menu)
        self.allow_commands.setChecked(self.state["allow_commands"])
        self._update_buttons()

    def _selected(self):
        rows = self.jobs.selectionModel().selectedRows()
        if not rows:
            return None
        job_id = self.jobs.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((j for j in self.state["jobs"] if j["id"] == job_id), None)

    def _update_buttons(self):
        job = self._selected()
        for key in ("edit", "preview", "toggle", "live", "delete"):
            self.buttons[key].setEnabled(job is not None)
        if job is not None:
            self.buttons["toggle"].setText("Turn off" if job["enabled"] else "Turn on")
            self.buttons["live"].setText("Back to dry run" if not job["dry_run"] else "Go live…")

    # ---- actions ----

    def _open_editor(self, job):
        dialog = JobDialog(self.win, job)
        if dialog.exec() == QDialog.Accepted:
            self.win.toast("Job saved" + (" as a dry run" if not job or job.get("id") is None else ""))
            self.reload()

    def _edit(self):
        job = self._selected()
        if job is not None:
            self._open_editor(job)

    def _save(self, job, success):
        body = {k: job[k] for k in ("id", "name", "enabled", "dry_run", "trigger", "destination", "template", "sources")}

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Automation", f"That didn't work: {error}")
            else:
                self.win.toast(success)
            self.reload()

        self.win.hub.post("/api/automation/save", body, done)

    def _toggle(self):
        job = self._selected()
        if job is not None:
            self._save({**job, "enabled": not job["enabled"]}, "Job turned " + ("off" if job["enabled"] else "on"))

    def _live(self):
        job = self._selected()
        if job is None:
            return
        if not job["dry_run"]:
            self._save({**job, "dry_run": True}, "Job is a dry run again: it won't send")
            return
        dest = destination_text(job["destination"], self.win)
        broadcast = "to" not in job["destination"]
        if QMessageBox.question(
            self, "Go live",
            f"Let “{job['name']}” transmit?\n\nIt will send {job['schedule']} to {dest}."
            + ("\n\nThat's a broadcast: every node on the channel receives it, and many will rebroadcast it."
               if broadcast else "")
            + "\n\nLimits still apply: at most every 6 hours for this job, 4 automated sends a day in total, "
              "never while the channel is busy or transmitting is off.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) == QMessageBox.Yes:
            self._save({**job, "dry_run": False}, f"“{job['name']}” is live")

    def _preview(self):
        job = self._selected()
        if job is None:
            return

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Preview", str(error))
            elif result["problem"]:
                QMessageBox.information(self, "Preview", f"This run would be skipped: {result['problem']}")
            else:
                QMessageBox.information(self, "Preview", f"{result['text']}\n\n({result['bytes']} of {result['limit']} "
                                                         "bytes; nothing was sent)")

        self.win.hub.post("/api/automation/preview", job, done)

    def _delete(self):
        job = self._selected()
        if job is not None and QMessageBox.question(
                self, "Delete job", f"Delete “{job['name']}”? Its past runs stay in the log.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            self.win.hub.post("/api/automation/delete", {"id": job["id"]}, lambda r, e: self.reload())

    def _toggle_commands(self, on):
        if on and QMessageBox.question(
                self, "Local commands",
                "Allow jobs to run commands on this computer? A command source runs with the logger's permissions "
                "each time a job that uses it runs.", QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel) != QMessageBox.Yes:
            self.allow_commands.setChecked(False)
            return
        self.win.hub.post("/api/automation/settings", {"allow_commands": bool(on)}, lambda r, e: self.reload())
