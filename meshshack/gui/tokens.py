"""Other apps tab: requests waiting for your approval, app tokens, and what apps have done.

Everything here goes straight to the database (tokens, approvals are decided through the logger's
API so an approved change runs exactly as the app asked, with the app's own authority)."""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .common import fmt_ago


class NewTokenDialog(QDialog):
    def __init__(self, parent, store):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("New API token")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. weather-display")
        self.send = QCheckBox("Can send direct messages (read access is always included)")
        self.broadcast = QCheckBox("Can also broadcast on channels")
        self.config = QCheckBox("Can change settings (restricted)")
        self.config.setToolTip("Right away: turn transmitting off, favorite/ignore nodes, alert keywords, coverage "
                               "notes, dry-run automation jobs. Anything else it asks for (transmit on, reboot, name, "
                               "role, position, channels, taking a job live) waits for your approval here.")
        self.broadcast.setEnabled(False)
        self.send.toggled.connect(lambda on: (self.broadcast.setEnabled(on), on or self.broadcast.setChecked(False)))
        note = QLabel("Every send from a token goes through the airtime gatekeeper with its own budget "
                      "(6 credits an hour, 30 s apart, paused when the channel is busy). Tokens can never "
                      "change the radio's settings or the transmit switch.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow(self.send)
        form.addRow(self.broadcast)
        form.addRow(self.config)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.token = None

    def _create(self):
        scopes = {"read"} | ({"send"} if self.send.isChecked() else set()) | ({"config"} if self.config.isChecked() else set())
        try:
            self.token = self.store.create_token(self.name.text().strip(), scopes,
                                                 allow_broadcast=self.broadcast.isChecked())
        except ValueError as ex:
            QMessageBox.warning(self, "API token", str(ex))
            return
        self.accept()


class ShowTokenDialog(QDialog):
    def __init__(self, parent, name, token):
        super().__init__(parent)
        self.setWindowTitle(f"Token for {name}")
        field = QLineEdit(token)
        field.setReadOnly(True)
        field.setMinimumWidth(460)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(token))
        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(copy)
        note = QLabel("This is the only time it's shown: MeshShack keeps just a hash. Give it to the app as "
                      "<code>Authorization: Bearer &lt;token&gt;</code>; the API is on http://127.0.0.1:8765.")
        note.setWordWrap(True)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(note)
        layout.addWidget(close)


class TokensGroup(QGroupBox):
    COLUMNS = ["App", "Can", "Created", "Last used", "Status"]

    def __init__(self, win):
        super().__init__("Other apps (API tokens)")
        self.win = win
        self.store = win.store
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(140)
        new = QPushButton("New token…")
        new.clicked.connect(self._new)
        self.revoke = QPushButton("Revoke…")
        self.revoke.clicked.connect(self._revoke)
        self.table.itemSelectionChanged.connect(self._update)
        buttons = QHBoxLayout()
        buttons.addWidget(new)
        buttons.addWidget(self.revoke)
        buttons.addStretch(1)
        intro = QLabel("Every token can read the log; it can also be allowed to send (under the airtime limits) "
                       "and to change settings (restricted, see above).")
        intro.setStyleSheet("color: gray")
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        tokens = self.store.tokens()
        self.table.setRowCount(len(tokens))
        for r, t in enumerate(tokens):
            scopes = t["scopes"].split(",")
            can = ", ".join(["read"] + (["send"] + (["broadcast"] if t["allow_broadcast"] else []) if "send" in scopes else [])
                            + (["settings (restricted)"] if "config" in scopes else []))
            cells = [t["name"], can, time.strftime("%Y-%m-%d", time.localtime(t["created_at"])),
                     fmt_ago(t["last_used_at"]) if t["last_used_at"] else "never",
                     "revoked" if t["revoked_at"] else "active"]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, t["name"])
                if t["revoked_at"]:
                    item.setForeground(Qt.gray)
                self.table.setItem(r, c, item)
        self._update()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        name = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((t for t in self.store.tokens() if t["name"] == name), None)

    def _update(self):
        t = self._selected()
        self.revoke.setEnabled(t is not None and not t["revoked_at"])

    def _new(self):
        dialog = NewTokenDialog(self, self.store)
        if dialog.exec() == QDialog.Accepted:
            self.refresh()
            ShowTokenDialog(self, dialog.name.text().strip(), dialog.token).exec()

    def _revoke(self):
        t = self._selected()
        if t is not None and QMessageBox.question(
                self, "Revoke token", f"Revoke {t['name']}'s token? That app loses access immediately.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            self.store.revoke_token(t["name"])
            self.refresh()


class ApprovalsGroup(QGroupBox):
    """Changes other apps asked for. Nothing happens until you approve; requests expire after a day."""

    COLUMNS = ["Asked", "App", "Wants to", "Status"]

    def __init__(self, win):
        super().__init__("Requests waiting for you")
        self.win = win
        self.store = win.store
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        self.approve = QPushButton("Approve…")
        self.approve.clicked.connect(lambda: self._decide(True))
        self.deny = QPushButton("Deny")
        self.deny.clicked.connect(lambda: self._decide(False))
        self.table.itemSelectionChanged.connect(self._update)
        buttons = QHBoxLayout()
        buttons.addWidget(self.approve)
        buttons.addWidget(self.deny)
        buttons.addStretch(1)
        intro = QLabel("Apps with the settings permission can change a few harmless things themselves; anything "
                       "else they ask for waits here. Approved, it runs exactly as asked.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray")
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self._seen_pending = {r["id"] for r in self.store.approvals(pending_only=True)}
        win.dataChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        rows = self.store.approvals(limit=100)
        self.table.setRowCount(len(rows))
        for r, a in enumerate(rows):
            cells = [time.strftime("%b %-d %H:%M", time.localtime(a["at"])), a["token_name"], a["summary"],
                     a["status"].upper() if a["status"] == "pending" else a["status"]]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, a["id"])
                if a["status"] != "pending":
                    item.setForeground(Qt.gray)
                self.table.setItem(r, c, item)
        pending = [a for a in rows if a["status"] == "pending"]
        new = [a for a in pending if a["id"] not in self._seen_pending]
        self._seen_pending |= {a["id"] for a in new}
        for a in new:  # tell the user, even with the window hidden
            if self.win.tray is not None:
                from .tray import app_icon
                self.win.tray.showMessage(f"MeshShack: {a['token_name']} asks for approval", a["summary"], app_icon(), 20000)
        self.setTitle(f"Requests waiting for you ({len(pending)})" if pending else "Requests waiting for you")
        tabs = getattr(self.win, "tabs", None)
        page = getattr(self.win, "other_apps", None)
        if tabs is not None and page is not None:
            tabs.setTabText(tabs.indexOf(page), f"Other apps ({len(pending)})" if pending else "Other apps")
        self._update()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        approval_id = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((a for a in self.store.approvals(limit=100) if a["id"] == approval_id), None)

    def _update(self):
        a = self._selected()
        pending = a is not None and a["status"] == "pending"
        self.approve.setEnabled(pending)
        self.deny.setEnabled(pending)

    def _decide(self, approve):
        a = self._selected()
        if a is None:
            return
        if approve and QMessageBox.question(
                self, "Approve request", f"{a['token_name']} asks to:\n\n{a['summary']}\n\nApprove it? It runs right away.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return

        def done(result, error):
            if error:
                QMessageBox.warning(self, "Request", f"That didn't work: {error}")
            elif result.get("status") == "failed":
                QMessageBox.warning(self, "Request", f"Approved, but it failed: {result.get('error')}")
            else:
                self.win.toast(f"Request {result.get('status')}")
            self.refresh()

        self.win.hub.post("/api/approvals/decide", {"id": a["id"], "approve": approve}, done)


class OtherAppsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        from PySide6.QtWidgets import QScrollArea
        self.approvals = ApprovalsGroup(win)
        self.tokens = TokensGroup(win)
        activity = QGroupBox("What apps have sent recently")
        self.activity = QTableWidget(0, 5)
        self.activity.setHorizontalHeaderLabels(["Time", "App", "Kind", "Result", "Note"])
        self.activity.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.activity.verticalHeader().setVisible(False)
        self.activity.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.activity.horizontalHeader().setStretchLastSection(True)
        self.activity.setMinimumHeight(140)
        QVBoxLayout(activity).addWidget(self.activity)
        info = QLabel("Apps on this computer use the API at <b>http://127.0.0.1:8765</b> with a token from below "
                      "(<code>Authorization: Bearer &lt;token&gt;</code>). Endpoints and limits: README, "
                      "“API for other apps”. Channel keys, tokens and approvals are never available to apps.")
        info.setWordWrap(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(info)
        layout.addWidget(self.approvals)
        layout.addWidget(self.tokens)
        layout.addWidget(activity)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.store = win.store
        win.dataChanged.connect(self._fill_activity)
        self._fill_activity()

    def _fill_activity(self):
        rows = [r for r in self.store.tx_log(limit=300) if r["source"].startswith("api:")][:50]
        self.activity.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, text in enumerate((time.strftime("%b %-d %H:%M", time.localtime(r["at"])), r["source"][4:], r["kind"],
                                      "sent" if r["allowed"] else "refused", r["reason"] or "")):
                self.activity.setItem(i, c, QTableWidgetItem(text))
