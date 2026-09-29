"""Chat tab: conversation list (channels and direct messages), thread view, and send box."""

import time
from html import escape

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSplitter,
    QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from ..api import MAX_TEXT_BYTES
from .common import fmt_ago, fmt_clock, fmt_day, node_name

# Offered by React; any single emoji can also arrive from other apps.
REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "✅", "👎"]

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
        self._opened_marker = None  # read marker when the current conversation was opened (for "New messages")
        self._scroll_target = None  # where the thread view should end up once its layout settles
        self._scroll_deadline = 0.0
        self._scroll_when_shown = False
        self._sending = False
        self._reply_to = None  # packet id of the message being replied to

        self.conv_list = QListWidget()
        self.conv_list.setMinimumWidth(180)
        self.conv_list.currentItemChanged.connect(self._on_select)

        self.header = QLabel()
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        # QTextBrowser lays text out after setHtml returns, so the scroll range grows afterwards;
        # keep applying the target while it does (see _scroll_to).
        self.view.verticalScrollBar().rangeChanged.connect(lambda *_: self._apply_scroll())
        self.view.anchorClicked.connect(self._link_clicked)

        # "Replying to …" bar above the send box, while a reply is being written.
        self.reply_label = QLabel()
        self.reply_label.setStyleSheet("color: gray")
        cancel_reply = QToolButton()
        cancel_reply.setText("✕")
        cancel_reply.setToolTip("Cancel the reply (Esc)")
        cancel_reply.clicked.connect(self._clear_reply)
        self.reply_bar = QWidget()
        reply_layout = QHBoxLayout(self.reply_bar)
        reply_layout.setContentsMargins(0, 0, 0, 0)
        reply_layout.addWidget(self.reply_label, 1)
        reply_layout.addWidget(cancel_reply)
        self.reply_bar.hide()

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
        right_layout.addWidget(self.reply_bar)
        right_layout.addLayout(send_row)
        QShortcut(QKeySequence(Qt.Key_Escape), self.input, activated=self._clear_reply)

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
        if self._scroll_when_shown:  # rendered while hidden (e.g. started in the tray): lay out, then scroll
            self._scroll_when_shown = False
            self._scroll_to(self._scroll_target)

    def _render_thread(self):
        kind, key = self.current
        switched = self._rendered is None or self._rendered[0] != self.current
        if switched:
            # Remember how far you'd read before this view marks the conversation read.
            self._opened_marker = self._read_marker(self.current)
            self._clear_reply()
        rows = self.store.thread(channel=key) if kind == "channel" else self.store.thread(peer=key)
        if rows and self._is_being_read():
            self.win.settings.setValue(f"read/{conv_key(kind, key)}", rows[-1]["id"])

        self._render_header()
        signature = tuple((r["id"], r["status"]) for r in rows)
        if self._rendered == (self.current, signature):
            return
        self._rendered = (self.current, signature)

        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        position = bar.value()
        self.view.setHtml(self._thread_html(rows, self._opened_marker))
        if switched:
            self._scroll_to(self._opening_target(rows, self._opened_marker))
        else:  # new messages: stay at the bottom if you were there, otherwise don't move
            self._scroll_to(("bottom",) if at_bottom else ("position", position))

    @staticmethod
    def _opening_target(rows, marker):
        """Where a conversation opens: with unread messages, the last one you'd read at the top
        (the unread ones follow below the "New messages" line); otherwise the newest at the bottom."""
        unread = [m for m in rows if m["id"] > (marker or 0) and m["direction"] == "in"]
        if not unread:
            return ("bottom",)
        read = [m for m in rows if m["id"] <= (marker or 0)]
        return ("anchor", f"m{read[-1]['id']}") if read else ("top",)

    def _scroll_to(self, target):
        self._scroll_target = target
        self._scroll_deadline = time.monotonic() + 1.0
        if not self.view.isVisible():
            self._scroll_when_shown = True
        self._apply_scroll()
        QTimer.singleShot(0, self._apply_scroll)

    def _apply_scroll(self):
        target = self._scroll_target
        if target is None or time.monotonic() > self._scroll_deadline:
            return
        bar = self.view.verticalScrollBar()
        if target[0] == "bottom":
            bar.setValue(bar.maximum())
        elif target[0] == "top":
            bar.setValue(0)
        elif target[0] == "anchor":
            self.view.scrollToAnchor(target[1])  # puts the anchor at the top of the view
        else:
            bar.setValue(min(target[1], bar.maximum()))

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

    def _thread_html(self, rows, unread_after=None):
        """unread_after: read marker at opening; a "New messages" line goes above the first unread."""
        dark = self.palette().color(QPalette.Window).lightness() < 128
        new_color = "#f28b82" if dark else "#c5221f"
        # Only between read and unread: with nothing read yet, a line above everything is noise.
        divider_done = (unread_after is None or not any(m["id"] <= unread_after for m in rows)
                        or not any(m["id"] > unread_after and m["direction"] == "in" for m in rows))
        in_bg, out_bg = ("#2d3138", "#1d4a80") if dark else ("#eceff3", "#d4e6ff")
        meta_color = "#9aa0a6" if dark else "#5f6368"
        if not rows:
            return f"<p style='color:{meta_color}' align='center'><br>No messages yet.</p>"

        # Reactions attach to the message they react to (by packet id); replies quote it.
        by_packet = {m["packet_id"]: m for m in rows if m["packet_id"] is not None}
        reactions = {}
        for m in rows:
            if m["emoji"] and m["reply_id"] in by_packet:
                reactions.setdefault(m["reply_id"], []).append(m)

        def who(m):
            return "You" if m["direction"] == "out" else (m["from_short"] or m["from_id"] or "?")

        # Reactions whose message isn't in this view: it may be in the log elsewhere (older, or another
        # conversation), or this station may never have received it.
        elsewhere = self.store.messages_by_packet(
            {m["reply_id"] for m in rows if m["emoji"] and m["reply_id"] and m["reply_id"] not in by_packet})

        def snippet(m, limit=60):
            text = " ".join((m["text"] or "").split())
            return text if len(text) <= limit else text[: limit - 1] + "…"

        link = f"style='color:{meta_color}; text-decoration:none'"
        parts = []
        last_day = None
        for m in rows:
            if not divider_done and m["id"] > unread_after and m["direction"] == "in":
                parts.append(f"<p align='center' style='color:{new_color}; font-weight:bold'>"
                             f"─────  New messages  ─────</p>")
                divider_done = True
            parts.append(f"<a name='m{m['id']}'></a>")
            if m["emoji"] and m["reply_id"] in by_packet:
                continue  # shown under the message it reacts to
            day = time.strftime("%Y-%m-%d", time.localtime(m["logged_at"]))
            if day != last_day:
                parts.append(f"<p align='center' style='color:{meta_color}'>{escape(fmt_day(m['logged_at']))}</p>")
                last_day = day
            outgoing = m["direction"] == "out"
            text = escape(m["text"] or "").replace("\n", "<br>")

            if m["emoji"] and m["reply_id"]:  # a reaction to a message that isn't in this view
                target = elsewhere.get(m["reply_id"])
                if target is not None:
                    what = f"reacted to {escape(who(target))}: “{escape(snippet(target))}”"
                else:
                    what = "reacted to a message this station never received"
                name = "You" if outgoing else f"<b>{escape(who(m))}</b>"
                bubble = (f"{name} <span style='font-size:large'>{text}</span> "
                          f"<span style='color:{meta_color}'>{what}</span><br>"
                          f"<span style='color:{meta_color}; font-size:small'>{fmt_clock(m['logged_at'])}</span>")
                parts.append(
                    f"<table width='100%' cellspacing='0' cellpadding='2'><tr><td align='{'right' if outgoing else 'left'}'>"
                    f"<table bgcolor='{out_bg if outgoing else in_bg}' cellpadding='7' cellspacing='0'><tr><td>{bubble}"
                    f"</td></tr></table></td></tr></table>")
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
            meta_html = f"<span style='color:{meta_color}; font-size:small'>{escape(' · '.join(meta))}"
            if m["packet_id"] is not None:
                meta_html += (f" · <a href='reply:{m['packet_id']}' {link}>Reply</a>"
                              f" · <a href='react:{m['packet_id']}' {link}>React</a>")
            meta_html += "</span>"

            if m["alert_reason"] or m["portnum"] == "ALERT_APP":  # flagged as a possible emergency
                label = escape(m["alert_reason"] or "Alert message")
                text = f"<b style='color:{new_color}'>🚨 {label}</b><br>{text}"
            quote = ""
            if m["reply_id"]:
                target = by_packet.get(m["reply_id"])
                quoted = f"{escape(who(target))}: {escape(snippet(target))}" if target else "an earlier message"
                quote = f"<span style='color:{meta_color}; font-size:small'>↩ {quoted}</span><br>"
            chips = ""
            if m["packet_id"] in reactions:
                grouped = {}
                for r in reactions[m["packet_id"]]:
                    grouped.setdefault(r["text"], []).append(who(r))
                chips = "<br><span style='font-size:small'>" + " · ".join(
                    f"{escape(emoji)} {escape(', '.join(names))}" for emoji, names in grouped.items()) + "</span>"

            if outgoing:
                body = f"{quote}{text}{chips}<br>{meta_html}"
                align, bg = "right", out_bg
            else:
                sender = escape(m["from_short"] or m["from_id"] or "?")
                long_name = f" <span style='color:{meta_color}'>{escape(m['from_long'])}</span>" if m["from_long"] else ""
                body = f"<b>{sender}</b>{long_name}<br>{quote}{text}{chips}<br>{meta_html}"
                align, bg = "left", in_bg
            parts.append(
                f"<table width='100%' cellspacing='0' cellpadding='2'><tr><td align='{align}'>"
                f"<table bgcolor='{bg}' cellpadding='7' cellspacing='0'><tr><td>{body}</td></tr></table>"
                f"</td></tr></table>"
            )
        return "".join(parts)

    # ---- replies and reactions ----

    def _message(self, packet_id):
        kind, key = self.current
        rows = self.store.thread(channel=key) if kind == "channel" else self.store.thread(peer=key)
        return next((m for m in rows if m["packet_id"] == packet_id), None)

    def _link_clicked(self, url):
        action, _, value = url.toString().partition(":")
        try:
            packet_id = int(value)
        except ValueError:
            return
        if action == "reply":
            self.start_reply(packet_id)
        elif action == "react":
            menu = QMenu(self)
            for emoji in REACTIONS:
                menu.addAction(emoji, lambda e=emoji: self.react(packet_id, e))
            menu.exec(QCursor.pos())

    def start_reply(self, packet_id):
        m = self._message(packet_id)
        if m is None:
            return
        name = "yourself" if m["direction"] == "out" else (m["from_short"] or m["from_id"] or "?")
        text = " ".join((m["text"] or "").split())
        self._reply_to = packet_id
        self.reply_label.setText(f"↩ Replying to {name}: {text[:80]}{'…' if len(text) > 80 else ''}")
        self.reply_bar.show()
        self.input.setFocus()

    def _clear_reply(self):
        self._reply_to = None
        self.reply_bar.hide()

    def react(self, packet_id, emoji):
        """Send a reaction. It's a small message like any other, so it goes through the gatekeeper."""
        body = {"text": emoji, "reply_id": packet_id, "emoji": True, **self._destination()}

        def done(result, error):
            if error:
                self.win.toast(f"Reaction not sent: {error}", 10000)
            elif result.get("warning"):
                self.win.show_result("Reaction sent", result)
            self.win._poll_data()

        self.win.hub.post("/api/send", body, done)

    def _destination(self):
        kind, key = self.current
        return {"to": key} if kind == "dm" else {"channel": key}

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
        body = {"text": self.input.text(), **self._destination()}
        if self._reply_to is not None:
            body["reply_id"] = self._reply_to
        self._sending = True
        self._update_send_state()

        def done(result, error):
            self._sending = False
            if error:
                self.win.toast(f"Not sent: {error}", 10000)
            else:
                self.input.clear()
                self._clear_reply()
                if result.get("warning"):
                    self.win.show_result("Sent", result)
            self._update_send_state()
            self.win._poll_data()

        self.win.hub.post("/api/send", body, done)
