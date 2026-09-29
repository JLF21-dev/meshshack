"""Device tab section: tokens for other apps using the local API (same as `meshshack token`)."""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
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
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.token = None

    def _create(self):
        scopes = {"read", "send"} if self.send.isChecked() else {"read"}
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
        intro = QLabel("Apps on this computer can read the log, and optionally send, through the local API.")
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
            can = "read" if t["scopes"] == "read" else "read, send" + (", broadcast" if t["allow_broadcast"] else "")
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
