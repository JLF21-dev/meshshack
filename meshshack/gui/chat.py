"""Chat tab: conversation list (channels and direct messages), thread view, and send box."""

import time
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..api import MAX_TEXT_BYTES
from .common import fmt_ago, fmt_clock, fmt_day, node_name

STATUS_MARKS = {
    "sending": ("…", "sending"),
    "relayed": ("✓", "relayed by a neighbor"),
    "delivered": ("✓✓", "delivered"),
    "failed": ("✗", "failed"),
}


def conv_key(kind, key):
    return f"{kind}:{key}"


class ChatTab(QWidget):
    unreadChanged = Signal(int)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store
        self.current = ("channel", 0)
        self.extra_dms = set()  # DM conversations opened from the map/nodes before any message exists
        self._rendered = None  # (conversation, signature) of what the thread view shows
        self._sending = False

        self.conv_list = QListWidget()
        self.conv_list.setMinimumWidth(180)
        self.conv_list.currentItemChanged.connect(self._on_select)

        self.header = QLabel()
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a message…")
        self.input.textChanged.connect(self._update_send_state)
        self.input.returnPressed.connect(self._send)
        self.counter = QLabel()
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send)

        send_row = QHBoxLayout()
        send_row.addWidget(self.input, 1)
        send_row.addWidget(self.counter)
        send_row.addWidget(self.send_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.header)
        right_layout.addWidget(self.view, 1)
        right_layout.addLayout(send_row)

        splitter = QSplitter()
        splitter.addWidget(self.conv_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 800])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        win.dataChanged.connect(self.refresh)
        win.statusChanged.connect(lambda _: self.refresh())
        self._update_send_state()

    # ---- conversations ----

    def open_dm(self, num):
        self.extra_dms.add(num)
        self.current = ("dm", num)
        self.refresh()
        self.input.setFocus()

    def _conversation_title(self, kind, key):
        if kind == "channel":
            for ch in self.win.status.get("channels", []):
                if ch["index"] == key:
                    return f"# {ch['name']}"
            return f"# Channel {key}"
        row = self.store.node(key)
        name = node_name(row, key)
        return f"@ {name} — {row['long_name']}" if row is not None and row["long_name"] else f"@ {name}"

    def _conversations(self):
        """[(kind, key)]: radio's channels first, then anything else with messages, newest first."""
        convs = [("channel", ch["index"]) for ch in self.win.status.get("channels", [])] or [("channel", 0)]
        for row in self.store.conversations():
            conv = (row["kind"], row["key"])
            if conv not in convs:
                convs.append(conv)
        for num in sorted(self.extra_dms):
            if ("dm", num) not in convs:
                convs.append(("dm", num))
        if self.current not in convs:
            convs.append(self.current)
        return convs

    def _read_marker(self, conv):
        return int(self.win.settings.value(f"read/{conv_key(*conv)}", 0))

    def _unread(self, conv):
        kind, key = conv
        marker = self._read_marker(conv)
        if kind == "channel":
            return self.store.unread_count(channel=key, after_id=marker)
        return self.store.unread_count(peer=key, after_id=marker)

    # ---- refresh ----

    def refresh(self):
        convs = self._conversations()
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        total_unread = 0
        for conv in convs:
            unread = 0 if conv == self.current and self._is_being_read() else self._unread(conv)
            total_unread += unread
            title = self._conversation_title(*conv)
            item = QListWidgetItem(f"{title}  ({unread})" if unread else title)
            item.setData(Qt.UserRole, conv)
            if unread:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.conv_list.addItem(item)
            if conv == self.current:
                self.conv_list.setCurrentItem(item)
        self.conv_list.blockSignals(False)
        self._render_thread()
        self.unreadChanged.emit(total_unread)
        self._update_send_state()

    def _is_being_read(self):
        return self.isVisible() and self.window().isActiveWindow()

    def _on_select(self, item, _previous):
        if item is not None:
            self.current = item.data(Qt.UserRole)
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _render_thread(self):
        kind, key = self.current
        rows = self.store.thread(channel=key) if kind == "channel" else self.store.thread(peer=key)
        if rows and self._is_being_read():
            self.win.settings.setValue(f"read/{conv_key(kind, key)}", rows[-1]["id"])

        self._render_header()
        signature = tuple((r["id"], r["status"]) for r in rows)
        if self._rendered == (self.current, signature):
            return
        switched = self._rendered is None or self._rendered[0] != self.current
        self._rendered = (self.current, signature)

        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        position = bar.value()
        self.view.setHtml(self._thread_html(rows))
        bar.setValue(bar.maximum() if switched or at_bottom else position)

    def _render_header(self):
        kind, key = self.current
        title = escape(self._conversation_title(kind, key))
        if kind == "channel":
            detail = f"channel {key} · broadcast to everyone on this channel"
        else:
            row = self.store.node(key)
            detail = f"direct message · !{key:08x}"
            if row is not None:
                detail += f" · heard {fmt_ago(row['last_heard'])}"
                if row["hops_away"] is not None:
                    detail += f" · {row['hops_away']} hop{'s' if row['hops_away'] != 1 else ''} away"
        self.header.setText(f"<b style='font-size:14px'>{title}</b><br><span style='color:gray'>{escape(detail)}</span>")

    def _thread_html(self, rows):
        dark = self.palette().color(QPalette.Window).lightness() < 128
        in_bg, out_bg = ("#2d3138", "#1d4a80") if dark else ("#eceff3", "#d4e6ff")
        meta_color = "#9aa0a6" if dark else "#5f6368"
        if not rows:
            return f"<p style='color:{meta_color}' align='center'><br>No messages yet.</p>"

        parts = []
        last_day = None
        for m in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(m["logged_at"]))
            if day != last_day:
                parts.append(f"<p align='center' style='color:{meta_color}'>{escape(fmt_day(m['logged_at']))}</p>")
                last_day = day
            outgoing = m["direction"] == "out"
            text = escape(m["text"] or "").replace("\n", "<br>")

            if m["emoji"] and m["reply_id"]:  # a tapback reaction
                who = "You" if outgoing else escape(m["from_short"] or m["from_id"])
                parts.append(f"<p align='center' style='color:{meta_color}'>{who} reacted {text}</p>")
                continue

            meta = [fmt_clock(m["logged_at"])]
            if outgoing:
                mark, label = STATUS_MARKS.get(m["status"], ("", m["status"] or ""))
                meta.append(f"{mark} {label}" + (f": {m['status_detail']}" if m["status_detail"] else ""))
            elif m["via_mqtt"]:
                # The signal is the MQTT gateway's retransmission, so it says nothing about the sender.
                meta.append("via internet (MQTT)")
            else:
                signal = []
                if m["rx_snr"] is not None:
                    signal.append(f"SNR {m['rx_snr']:.1f} dB")
                if m["rx_rssi"] is not None:
                    signal.append(f"RSSI {m['rx_rssi']}")
                if m["hops"] == 0:
                    meta.extend(["direct", *signal])
                elif m["hops"] is not None:
                    # A relayed message's signal is the last relay's, so say so.
                    meta.append(f"{m['hops']} hop{'s' if m['hops'] != 1 else ''}")
                    if signal:
                        meta.append("last relay " + ", ".join(signal))
                else:
                    meta.extend(signal)
            meta_html = f"<span style='color:{meta_color}; font-size:small'>{escape(' · '.join(meta))}</span>"

            if outgoing:
                body = f"{text}<br>{meta_html}"
                align, bg = "right", out_bg
            else:
                sender = escape(m["from_short"] or m["from_id"] or "?")
                long_name = f" <span style='color:{meta_color}'>{escape(m['from_long'])}</span>" if m["from_long"] else ""
                body = f"<b>{sender}</b>{long_name}<br>{text}<br>{meta_html}"
                align, bg = "left", in_bg
            parts.append(
                f"<table width='100%' cellspacing='0' cellpadding='2'><tr><td align='{align}'>"
                f"<table bgcolor='{bg}' cellpadding='7' cellspacing='0'><tr><td>{body}</td></tr></table>"
                f"</td></tr></table>"
            )
        return "".join(parts)

    # ---- sending ----

    def _update_send_state(self):
        size = len(self.input.text().encode("utf-8"))
        over = size > MAX_TEXT_BYTES
        self.counter.setText(f"{size}/{MAX_TEXT_BYTES}")
        self.counter.setStyleSheet("color: #d93025; font-weight: bold" if over else "")
        ready = self.win.radio_ready
        self.send_button.setEnabled(ready and 0 < len(self.input.text().strip()) and not over and not self._sending)
        self.input.setPlaceholderText("Type a message…" if ready else "Radio not connected — messages can't be sent")

    def _send(self):
        if not self.send_button.isEnabled():
            return
        kind, key = self.current
        body = {"text": self.input.text()}
        if kind == "dm":
            body["to"] = key
        else:
            body["channel"] = key
        self._sending = True
        self._update_send_state()

        def done(result, error):
            self._sending = False
            if error:
                self.win.toast(f"Not sent: {error}", 10000)
            else:
                self.input.clear()
                if result.get("warning"):
                    self.win.show_result("Sent", result)
            self._update_send_state()
            self.win._poll_data()

        self.win.hub.post("/api/send", body, done)
