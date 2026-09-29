"""Desktop app: request-result formatting and an offscreen smoke test of the main window."""

import os
import tempfile
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Keep the app's QSettings (read markers, window layout) out of the real ~/.config.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="meshshack-test-config-")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")

from meshshack.gui.app import MainWindow  # noqa: E402  (imports QtWebEngine before QApplication)
from PySide6.QtWidgets import QApplication  # noqa: E402

from meshshack.gui.activity import ActivityTracker  # noqa: E402
from meshshack.gui.nodes import COLUMNS  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from meshshack.store import path_kind  # noqa: E402
from meshshack.store import BROADCAST_NUM, Store  # noqa: E402

ME, RELAY, DEST = 0xA1B2C3D4, 0x11111111, 0x33333333


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    for num, short in ((ME, "N0C"), (RELAY, "RLY"), (DEST, "DST")):
        s.record_node_info({"num": num, "user": {"id": f"!{num:08x}", "shortName": short}})
    yield s
    s.close()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_traceroute_result(store, qapp):
    store.record_request(4242, "traceroute", DEST)
    tracker = ActivityTracker(store)
    store.record_packet({"from": DEST, "to": ME, "id": 1, "decoded": {
        "portnum": "TRACEROUTE_APP", "requestId": 4242,
        "traceroute": {"route": [RELAY], "snrTowards": [24, -13], "routeBack": [RELAY], "snrBack": [10, -128]}}})
    tracker.check()
    assert tracker.entries[0][1] == ("Traceroute to DST: N0C → RLY (6.0 dB) → DST (-3.2 dB)"
                                     "  |  back: DST → RLY (2.5 dB) → N0C")


def test_request_failure_and_timeout(store, qapp, monkeypatch):
    store.record_request(1, "position", DEST, now=time.time() - 1)
    store.record_request(2, "telemetry", DEST)
    tracker = ActivityTracker(store)
    store.record_packet({"from": ME, "to": ME, "id": 5, "decoded": {
        "portnum": "ROUTING_APP", "requestId": 1, "routing": {"errorReason": "NO_RESPONSE"}}})
    tracker.check()
    texts = [text for _, text in tracker.entries]
    assert "Position request to DST failed: NO_RESPONSE" in texts
    assert "Telemetry request to DST sent…" in texts

    later = time.time() + 600
    monkeypatch.setattr(time, "time", lambda: later)
    tracker.check()
    assert "Telemetry request to DST: no reply after 3 minutes" in [text for _, text in tracker.entries]

    tracker.fail("traceroute", DEST, "radio not connected")  # never reached the hub
    assert tracker.entries[0][1] == "Traceroute to DST not sent: radio not connected"


def test_main_window_without_logger(store, qapp, tmp_path):
    store.record_packet({"from": DEST, "to": BROADCAST_NUM, "id": 9, "channel": 0,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello <b>mesh</b>"}})
    store.record_outgoing_message(10, ME, DEST, 0, "direct reply")

    win = MainWindow(tmp_path / "test.db")
    try:
        deadline = time.time() + 5
        while "not running" not in win.connection_label.text() and time.time() < deadline:
            qapp.processEvents()
        assert "Logger not running" in win.connection_label.text()

        html = win.chat.view.toHtml()
        assert "hello &lt;b&gt;mesh&lt;/b&gt;" in html  # message text is escaped
        assert not win.chat.send_button.isEnabled()

        titles = [win.chat.conv_list.item(i).text() for i in range(win.chat.conv_list.count())]
        assert any(t.startswith("@ DST") for t in titles)

        win.open_dm(DEST)
        assert "direct reply" in win.chat.view.toPlainText()

        assert win.nodes.table.rowCount() == 1  # default window: heard in the last 24 hours
        win.nodes.window_box.setCurrentIndex(3)  # all time, including never-heard nodes
        assert win.nodes.table.rowCount() == 3  # nobody is "me" without a connected radio
    finally:
        win.close()


def test_map_shape_follows_how_a_node_was_heard():
    assert path_kind(5, 0) == "radio"
    assert path_kind(0, 3) == "mqtt"
    assert path_kind(2, 1) == "both"
    assert path_kind(0, 0) == "unknown"


def test_rounded_positions_are_recognized_and_boxed():
    from meshshack.gui.common import effective_precision, infer_precision_bits, precision_cell

    # The UIUC Electrical and Computer Engineering Building (40.1149296, -88.2280589), rounded to
    # 13 bits as a LongFast default channel shares it. Go Illini.
    lat, lon = 40.1342464, -88.211456
    assert infer_precision_bits(lat, lon) == 13
    (south, west), (north, east) = precision_cell(lat, lon, 13)
    assert south < 40.1149296 < north and west < -88.2280589 < east
    assert infer_precision_bits(40.1149296, -88.2280589) is None  # a real fix isn't mistaken for rounding
    assert effective_precision(lat, lon, 32) is None  # the packet says full precision
    assert effective_precision(40.1149296, -88.2280589, 16) == 16  # the packet's own value wins
    assert precision_cell(40.1149296, -88.2280589, None) is None


def test_station_position_prefers_exact_fixed_position(store):
    from meshshack.gui.common import station_position

    status = {"position": {"latitude": 40.1342464, "longitude": -88.211456, "fixed_position": True}}
    assert station_position(status, store, ME) == (40.1342464, -88.211456, 13)  # nothing stored yet
    store.set_station("fixed_position", {"latitude": 40.1149296, "longitude": -88.2280589, "altitude": 0})
    assert station_position(status, store, ME) == (40.1149296, -88.2280589, None)
    # Moved elsewhere (another cell) or no longer fixed: the stored value is stale, so ignore it.
    moved = {"position": {"latitude": 40.501248, "longitude": -88.211456, "fixed_position": True}}
    assert station_position(moved, store, ME)[0] == 40.501248
    unfixed = {"position": {**status["position"], "fixed_position": False}}
    assert station_position(unfixed, store, ME)[2] == 13


def test_transmit_switch_follows_the_database(store, qapp, tmp_path):
    from meshshack.airtime import Gatekeeper

    win = MainWindow(tmp_path / "test.db")
    try:
        assert "Transmit on" in win.tx_button.text()
        win._toggle_transmit()  # turning off needs no confirmation
        assert "OFF" in win.tx_button.text() and not Gatekeeper(store).transmit_enabled()
        Gatekeeper(store).set_transmit_enabled(True, by="command line")  # e.g. `meshshack tx on`
        win.dataChanged.emit()
        assert "Transmit on" in win.tx_button.text()
    finally:
        win.close()


def test_tray_keeps_the_app_running_when_the_window_closes(store, qapp, tmp_path):
    from meshshack.gui.tray import Tray, state_icon

    assert not state_icon(3, transmit_on=False, connected=False).isNull()
    win = MainWindow(tmp_path / "test.db")
    quits = []
    win.tray = Tray(win, lambda: quits.append(True))
    try:
        win.show()
        win.close()  # hides to the tray instead of quitting
        assert not win.isVisible() and not quits and not win.hub._closed
        assert "Radio not connected" in win.tray.toolTip() and "Transmitting" in win.tray.toolTip()
        win._toggle_transmit()
        assert "Transmit is OFF" in win.tray.toolTip() and not win.tray.transmit_action.isChecked()
    finally:
        win.quitting = True
        win.close()


def test_channel_dialogs(qapp):
    from meshshack.gui.channels import ChannelDialog, ShareDialog, precision_text

    assert precision_text(13, 40.11) == "Rounded: 5.8 km × 4.5 km area (13 bits)"
    assert precision_text(0, 40.11) == "Don't share position" and precision_text(32, 40.11) == "Exact position"

    add = ChannelDialog(None, 40.11)
    add.name.setText("Private")
    add._accept()
    assert add.body == {"name": "Private", "key": "random", "position_precision": 13}

    primary = {"index": 0, "name": "", "role": "PRIMARY", "encryption": "default", "position_precision": 13,
               "share_url": "https://meshtastic.org/e/?add=true#CgMSAQE"}
    edit = ChannelDialog(None, 40.11, primary)
    assert not edit.name.isEnabled() and not edit.key_mode.isEnabled()
    edit.precision.setCurrentIndex(edit.precision.findData(16))
    edit._accept()
    assert edit.body == {"position_precision": 16}  # only what changed, and never the primary's name/key

    from PySide6.QtWidgets import QLabel, QLineEdit

    share = ShareDialog(None, primary)
    qr = [label.pixmap() for label in share.findChildren(QLabel) if not label.pixmap().isNull()]
    assert qr and qr[0].width() > 100  # a real QR image
    assert share.findChild(QLineEdit).text() == primary["share_url"]


def test_node_charts_and_table(store, qapp):
    from meshshack.gui.charts import MetricChart, NodeCharts, downsample

    now = time.time()
    for i in range(3):
        store.record_packet({"from": DEST, "to": BROADCAST_NUM, "id": 100 + i, "rxSnr": 4.0 + i, "hopStart": 3,
                             "hopLimit": 3, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {
                                 "batteryLevel": 80 - i, "voltage": 3.9, "channelUtilization": 5.0 + i}}}},
                            now=now - 3600 + i * 600)
    charts = NodeCharts(store)
    charts.show()
    charts.set_node(DEST)
    titles = sorted(c.chart().title() for c in charts.findChildren(MetricChart))
    assert titles == ["Battery (%)", "Channel utilization (%)", "Direct SNR (dB)", "Voltage (V)"]
    charts.table_toggle.setChecked(True)
    assert charts.table.rowCount() == 12  # 3 readings x (battery, voltage, channel util, direct SNR)
    charts.set_node(RELAY)  # nothing logged
    assert charts.stack.currentWidget() is charts.empty

    points = [(float(t), float(t)) for t in range(10_000)]
    thinned = downsample(points, 100)
    assert len(thinned) == 100 and thinned[0][1] < thinned[-1][1]


def test_favorites_stay_on_top_and_details_show(store, qapp, tmp_path):
    now = time.time()
    store.record_packet({"from": RELAY, "to": BROADCAST_NUM, "id": 1, "decoded": {"portnum": "POSITION_APP"}}, now=now)
    store.record_packet({"from": DEST, "to": BROADCAST_NUM, "id": 2, "decoded": {"portnum": "POSITION_APP"}}, now=now - 600)
    store.set_node_flags(DEST, favorite=True)
    win = MainWindow(tmp_path / "test.db")
    try:
        nodes = win.nodes
        nodes.refresh()
        first = [nodes.table.item(r, 0).text() for r in range(nodes.table.rowCount())]
        assert first[0] == "★ DST"  # the favorite, though RELAY was heard more recently
        nodes.favorites_first.setChecked(False)
        assert nodes.table.item(0, 0).text() == "RLY"
        nodes.table.selectRow(1)
        html = nodes.details.toPlainText()
        assert "★ Favorite" in html and "position 1" in html
    finally:
        win.close()


def test_blanks_sort_last_in_both_directions(store, qapp, tmp_path):
    win = MainWindow(tmp_path / "test.db")
    try:
        nodes = win.nodes
        store.record_packet({"from": DEST, "to": BROADCAST_NUM, "id": 1, "rxSnr": 3.0, "hopStart": 3, "hopLimit": 3,
                             "decoded": {"portnum": "POSITION_APP"}})
        nodes.window_box.setCurrentIndex(3)  # all time: includes nodes with no SNR at all
        snr = COLUMNS.index("Direct SNR")
        for order in (Qt.AscendingOrder, Qt.DescendingOrder):
            nodes.table.sortByColumn(snr, order)
            nodes.refresh()
            assert nodes.table.item(0, snr).text() == "3.0", order
    finally:
        win.close()


def test_chat_opens_at_last_read_or_newest(store, qapp, tmp_path):
    def settle(ms=400):
        deadline = time.time() + ms / 1000
        while time.time() < deadline:
            qapp.processEvents()

    ids = []
    for i in range(40):
        ids.append(store.record_packet({"from": RELAY, "to": ME, "id": 500 + i, "decoded": {
            "portnum": "TEXT_MESSAGE_APP", "text": f"message {i}"}}, now=time.time() - 4000 + i * 60))
    rows = store.thread(peer=RELAY)
    win = MainWindow(tmp_path / "test.db")
    try:
        win.resize(900, 500)
        win.show()
        chat, bar = win.chat, win.chat.view.verticalScrollBar()

        # 15 read, 25 unread: the last read message is at the top, "New messages" below it.
        win.settings.setValue(f"read/dm:{RELAY}", rows[14]["id"])
        win.open_dm(RELAY)
        settle()
        assert 0 < bar.value() < bar.maximum()
        top = chat.view.cursorForPosition(chat.view.viewport().rect().topLeft()).block().text()
        assert "New messages" in chat.view.toPlainText()
        assert chat.view.toPlainText().index("message 14") < chat.view.toPlainText().index("New messages")
        assert "message 13" not in top  # scrolled past the older ones

        # Nothing unread: opens at the newest messages.
        win.open_dm(DEST)
        settle()
        win.settings.setValue(f"read/dm:{RELAY}", rows[-1]["id"])
        win.open_dm(RELAY)
        settle()
        assert bar.maximum() > 0 and bar.value() == bar.maximum()

        # At the bottom, a new message keeps you there; scrolled up, it doesn't move you.
        store.record_packet({"from": RELAY, "to": ME, "id": 999, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "new"}})
        win.dataChanged.emit()
        settle()
        assert bar.value() == bar.maximum()
        bar.setValue(10)
        store.record_packet({"from": RELAY, "to": ME, "id": 1000, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "newer"}})
        win.dataChanged.emit()
        settle()
        assert bar.value() == 10
    finally:
        win.close()


def test_replies_and_reactions_in_chat(store, qapp, tmp_path):
    from PySide6.QtCore import QUrl

    def text(pid, sender, body, reply=None, emoji=None):
        decoded = {"portnum": "TEXT_MESSAGE_APP", "text": body}
        if reply:
            decoded["replyId"] = reply
        if emoji:
            decoded["emoji"] = 1
        store.record_packet({"from": sender, "to": ME, "id": pid, "decoded": decoded})

    text(111, RELAY, "anyone up for a range test?")
    text(112, RELAY, "👍", reply=111, emoji=True)  # RELAY reacts to its own message
    store.record_outgoing_message(113, ME, RELAY, 0, "👍", reply_id=111, emoji=True)  # so do we
    text(114, RELAY, "ok, 7pm then", reply=111)
    text(115, RELAY, "😂", reply=99999, emoji=True)  # reacts to something not in this view

    win = MainWindow(tmp_path / "test.db")
    sent = []
    win.hub.post = lambda path, body, done=None: sent.append((path, body))
    try:
        win.show()
        win.open_dm(RELAY)
        plain = win.chat.view.toPlainText()
        assert "👍 RLY, You" in plain  # grouped under the message it reacts to
        assert "↩ RLY: anyone up for a range test?" in plain  # the reply quotes it
        assert "RLY 😂 reacted to a message this station never received" in plain
        assert plain.count("👍") == 1  # not also shown as separate lines

        win.chat._link_clicked(QUrl("reply:114"))
        assert win.chat.reply_bar.isVisibleTo(win) and "ok, 7pm then" in win.chat.reply_label.text()
        win.chat.input.setText("see you there")
        win.chat.send_button.setEnabled(True)  # no radio in this test
        win.chat._send()
        assert sent[-1] == ("/api/send", {"text": "see you there", "to": RELAY, "reply_id": 114})

        win.chat.start_reply(111)
        win.chat._clear_reply()  # Esc / ✕
        assert not win.chat.reply_bar.isVisibleTo(win) and win.chat._reply_to is None

        win.chat.react(114, "❤️")
        assert sent[-1] == ("/api/send", {"text": "❤️", "reply_id": 114, "emoji": True, "to": RELAY})
    finally:
        win.close()


def test_gui_logging_keeps_running_after_an_error(tmp_path, monkeypatch, qapp):
    import logging
    import sys

    from meshshack.gui import app as gui_app

    monkeypatch.setattr(gui_app, "LOG_DIR", tmp_path / "state")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)  # don't redirect pytest's output
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    logger = logging.getLogger("meshshack")
    before = list(logger.handlers)
    try:
        gui_app.setup_logging()
        try:
            raise ValueError("boom in a slot")
        except ValueError:
            sys.excepthook(*sys.exc_info())  # logged, not fatal
        for h in logger.handlers:
            h.flush()
        text = (tmp_path / "state" / "gui.log").read_text()
        assert "MeshShack app starting" in text and "boom in a slot" in text
    finally:
        for h in logger.handlers[len(before):]:
            logger.removeHandler(h)
        logger.propagate = True


def test_reaction_to_a_message_elsewhere_quotes_it(store, qapp, tmp_path):
    store.record_packet({"from": DEST, "to": BROADCAST_NUM, "id": 777, "channel": 0,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "net check-in at 8"}})
    store.record_packet({"from": RELAY, "to": ME, "id": 778, "decoded": {  # reacts, in a DM, to a channel message
        "portnum": "TEXT_MESSAGE_APP", "text": "👍", "replyId": 777, "emoji": 1}})
    win = MainWindow(tmp_path / "test.db")
    try:
        win.show()
        win.open_dm(RELAY)
        assert "RLY 👍 reacted to DST: “net check-in at 8”" in win.chat.view.toPlainText()
    finally:
        win.close()
