"""Channels tab: the radio's channels, with add / edit / delete and sharing by QR code or link.

Channel details include the encryption keys, so they come from the owner-only /api/channels
and are never stored by the app. Every change is a config write through the logger's airtime
gatekeeper (so the transmit kill switch covers it too).
"""

import base64
import io

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .common import cell_size_text

COLUMNS = ["#", "Name", "Role", "Encryption", "Position sharing"]
PRECISIONS = [0, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 32]
ENCRYPTION_TEXT = {
    "none": "None: anyone can read it",
    "default": "Default key: public, anyone can read it",
    "private-128": "Private key (AES-128)",
    "private-256": "Private key (AES-256)",
}
MAX_NAME_BYTES = 11


def precision_text(bits, lat):
    if bits == 0:
        return "Don't share position"
    if bits >= 32:
        return "Exact position"
    return f"Rounded: {cell_size_text(lat if lat is not None else 40.0, bits)} area ({bits} bits)"


def channel_name(ch, status_names=None):
    """What apps call a channel: its name, or for an unnamed primary the modem preset's name
    (e.g. LongFast), which the logger's status works out."""
    if ch["name"]:
        return ch["name"]
    return (status_names or {}).get(ch["index"]) or ("Primary" if ch["role"] == "PRIMARY" else f"Channel {ch['index']}")


class ChannelDialog(QDialog):
    """Add a channel, or edit one. For the primary channel only position sharing can change."""

    def __init__(self, parent, lat, channel=None):
        super().__init__(parent)
        self.channel = channel
        primary = channel is not None and channel["role"] == "PRIMARY"
        self.setWindowTitle("Edit channel" if channel else "Add channel")

        self.name = QLineEdit(channel["name"] if channel else "")
        self.name.setPlaceholderText(f"up to {MAX_NAME_BYTES} characters")
        self.key_mode = QComboBox()
        if channel:
            self.key_mode.addItem("Keep the current key", "keep")
        self.key_mode.addItem("New random private key (AES-256)", "random")
        self.key_mode.addItem("Paste a key (base64)", "paste")
        self.key_mode.addItem("Default public key (not private)", "default")
        self.key_mode.addItem("No encryption", "none")
        self.key = QLineEdit()
        self.key.setPlaceholderText("16- or 32-byte key, base64")
        self.key.setVisible(False)
        self.key_mode.currentIndexChanged.connect(lambda _: self.key.setVisible(self.key_mode.currentData() == "paste"))
        self.precision = QComboBox()
        for bits in PRECISIONS:
            self.precision.addItem(precision_text(bits, lat), bits)
        current = channel["position_precision"] if channel else 13
        self.precision.setCurrentIndex(max(0, self.precision.findData(current)))

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Encryption", self.key_mode)
        form.addRow("", self.key)
        form.addRow("Position sharing", self.precision)
        note = QLabel()
        note.setWordWrap(True)
        if primary:
            self.name.setEnabled(False)
            self.key_mode.setEnabled(False)
            note.setText("This is your primary channel: its name and key are what put you on the public mesh, "
                         "so only position sharing can be changed here. Position sharing sets how precisely "
                         "your location is shared with everyone on this channel.")
        else:
            note.setText("Share a private channel only with people you mean to: anyone with its link or QR "
                         "code can read and send on it.")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.body = None

    def _accept(self):
        body = {}
        primary = self.channel is not None and self.channel["role"] == "PRIMARY"
        if not primary:
            name = self.name.text().strip()
            if not name or len(name.encode("utf-8")) > MAX_NAME_BYTES:
                QMessageBox.warning(self, "Channel", f"Give the channel a name of 1 to {MAX_NAME_BYTES} characters.")
                return
            if self.channel is None or name != self.channel["name"]:
                body["name"] = name
            mode = self.key_mode.currentData()
            if mode == "paste":
                try:
                    if len(base64.b64decode(self.key.text().strip(), validate=True)) not in (16, 32):
                        raise ValueError
                except ValueError:
                    QMessageBox.warning(self, "Channel", "The key must be 16 or 32 bytes, written in base64.")
                    return
                body["key"] = self.key.text().strip()
            elif mode != "keep":
                body["key"] = mode
        bits = self.precision.currentData()
        if self.channel is None or bits != self.channel["position_precision"]:
            body["position_precision"] = bits
        self.body = body
        self.accept()


class ShareDialog(QDialog):
    def __init__(self, parent, channel):
        super().__init__(parent)
        import segno

        self.setWindowTitle(f"Share channel: {channel_name(channel)}")
        url = channel["share_url"]
        png = io.BytesIO()
        segno.make(url, error="m").save(png, kind="png", scale=6, border=3)
        pixmap = QPixmap()
        pixmap.loadFromData(png.getvalue())
        qr = QLabel()
        qr.setPixmap(pixmap)
        qr.setAlignment(Qt.AlignCenter)
        link = QLineEdit(url)
        link.setReadOnly(True)
        copy = QPushButton("Copy link")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(url))
        private = channel["encryption"].startswith("private")
        warning = QLabel(
            "Scan this in the Meshtastic app (or open the link) to add this channel. "
            + ("It includes the channel's key: anyone who has it can read and send on this channel, "
               "so share it only with people you trust." if private else
               "This channel isn't private, so anyone can already read it.")
        )
        warning.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(link, 1)
        row.addWidget(copy)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(qr)
        layout.addWidget(warning)
        layout.addLayout(row)
        layout.addWidget(close)


class ChannelsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.data = {"channels": [], "latitude": None}
        self._loaded_for = None

        intro = QLabel("Your radio's channels. The primary channel (#0) is the public mesh; secondary channels "
                       "are extra, usually private, conversations. Changing a channel is a config change, "
                       "so it goes through the transmit switch and airtime limits.")
        intro.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _: self._edit())

        self.buttons = {}
        row = QHBoxLayout()
        for key, label, handler in (("add", "Add channel…", self._add), ("edit", "Edit…", self._edit),
                                    ("share", "Share…", self._share), ("delete", "Delete…", self._delete)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            self.buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        self.message = QLabel()
        self.message.setStyleSheet("color: gray")

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(self.message)

        win.statusChanged.connect(self._status_changed)
        self._update_buttons()

    # ---- loading ----

    def _status_changed(self, status):
        # Channel names are in the status; reload the details when they change (or we reconnect).
        key = (status.get("connected"), tuple((c["index"], c["name"]) for c in status.get("channels", [])))
        if key != self._loaded_for and self.isVisible():
            self.reload()

    def showEvent(self, event):
        super().showEvent(event)
        self.reload()

    def reload(self):
        self._loaded_for = (self.win.status.get("connected"),
                            tuple((c["index"], c["name"]) for c in self.win.status.get("channels", [])))
        if not self.win.radio_ready:
            self.data = {"channels": [], "latitude": None}
            self.message.setText("The radio isn't connected, so its channels can't be shown.")
            self._fill()
            return

        def done(result, error):
            if error:
                self.message.setText(f"Couldn't load channels: {error}")
                return
            self.data = result
            self.message.setText("")
            self._fill()

        self.win.hub.get("/api/channels", done)

    def _fill(self):
        selected = self._selected()
        lat = self.data.get("latitude")
        channels = self.data.get("channels", [])
        self.table.setRowCount(len(channels))
        for r, ch in enumerate(channels):
            status_names = {c["index"]: c["name"] for c in self.win.status.get("channels", [])}
            cells = [str(ch["index"]), channel_name(ch, status_names), ch["role"].capitalize(),
                     ENCRYPTION_TEXT.get(ch["encryption"], ch["encryption"]),
                     precision_text(ch["position_precision"], lat)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, ch["index"])
                self.table.setItem(r, c, item)
            if selected is not None and selected["index"] == ch["index"]:
                self.table.selectRow(r)
        self._update_buttons()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((c for c in self.data.get("channels", []) if c["index"] == index), None)

    def _update_buttons(self):
        ch, ready = self._selected(), self.win.radio_ready
        self.buttons["add"].setEnabled(ready)
        self.buttons["edit"].setEnabled(ready and ch is not None)
        self.buttons["share"].setEnabled(ch is not None)
        self.buttons["delete"].setEnabled(ready and ch is not None and ch["role"] == "SECONDARY")

    # ---- actions ----

    def _post(self, path, body, success):
        def done(result, error):
            if error:
                QMessageBox.warning(self, "Channels", f"That didn't work: {error}")
            else:
                self.win.show_result(success, result)
            self.win.poll_status()
            self.reload()

        self.win.hub.post(path, body, done)

    def _add(self):
        dialog = ChannelDialog(self, self.data.get("latitude"))
        if dialog.exec() == QDialog.Accepted:
            self._post("/api/channels/add", dialog.body, f"Channel {dialog.body['name']!r} added")

    def _edit(self):
        ch = self._selected()
        if ch is None or not self.win.radio_ready:
            return
        dialog = ChannelDialog(self, self.data.get("latitude"), ch)
        if dialog.exec() == QDialog.Accepted:
            if not dialog.body:
                return
            self._post("/api/channels/update", {"index": ch["index"], **dialog.body}, "Channel updated")

    def _share(self):
        ch = self._selected()
        if ch is not None:
            ShareDialog(self, ch).exec()

    def _delete(self):
        ch = self._selected()
        if ch is None or ch["role"] != "SECONDARY":
            return
        if QMessageBox.question(
            self, "Delete channel",
            f"Delete channel {channel_name(ch)!r}?\n\nYou'll stop receiving it. If it has a private key and you "
            "haven't kept its share link, you'd need it from someone else to rejoin.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) == QMessageBox.Yes:
            self._post("/api/channels/delete", {"index": ch["index"]}, "Channel deleted")
