"""Main window: Chat / Map / Nodes / Coverage / Channels / Device tabs over the logger's database and API."""

import faulthandler
import logging
import logging.handlers
import os
import sys
from pathlib import Path

# QtWebEngine must be imported before the QApplication exists.
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
from PySide6.QtCore import QEvent, QSettings, QTimer, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox, QPushButton, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from ..airtime import Gatekeeper
from ..store import Store
from .activity import KIND_LABELS, ActivityTracker
from .alerts import AlertCenter, AlertsTab
from .automation import AutomationTab
from .channels import ChannelsTab
from .coverage import CoverageTab
from .chat import ChatTab
from .device import DeviceTab
from .hub_client import HubClient
from .map_tab import MapTab
from .nodes import NodesTab
from .tray import Tray, app_icon

DATA_POLL_MS = 700
STATUS_POLL_MS = 5000
CLOCK_TICK_MS = 30000  # refresh "5m ago" style labels


class MainWindow(QMainWindow):
    statusChanged = Signal(dict)
    dataChanged = Signal()

    def __init__(self, db_path):
        super().__init__()
        self.setWindowTitle("MeshShack")
        self.setWindowIcon(app_icon())
        self.tray = None  # set by run_gui when the desktop has a tray
        self.quitting = False
        self.store = Store(db_path)
        self.hub = HubClient(db_path, self)
        self.status = {"connected": False, "hub_error": "Checking…"}
        self.activity = ActivityTracker(self.store, self)
        self.settings = QSettings("meshshack", "meshshack")

        self.tabs = QTabWidget()
        self.chat = ChatTab(self)
        self.map = MapTab(self)
        self.nodes = NodesTab(self)
        self.coverage = CoverageTab(self)
        self.channels = ChannelsTab(self)
        self.alerts_tab = AlertsTab(self)
        self.automation = AutomationTab(self)
        self.device = DeviceTab(self)
        self.tabs.addTab(self.chat, "Chat")
        self.tabs.addTab(self.map, "Map")
        self.tabs.addTab(self.nodes, "Nodes")
        self.tabs.addTab(self.coverage, "Coverage")
        self.tabs.addTab(self.channels, "Channels")
        self.tabs.addTab(self.alerts_tab, "Alerts")
        self.tabs.addTab(self.automation, "Automation")
        self.tabs.addTab(self.device, "Device")
        # The emergency banner sits above the tabs, so it shows whichever tab is open.
        self.alert_center = AlertCenter(self)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.alert_center)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        QTimer.singleShot(0, self.alert_center.check)
        # Automation jobs that "notify me" show up as desktop notifications (only new ones).
        runs = self.store.automation_runs(limit=1)
        self._last_notified = runs[0]["id"] if runs else 0
        self.dataChanged.connect(self._show_automation_notifications)
        self.chat.unreadChanged.connect(self._show_unread)

        self.connection_label = QLabel()
        self.statusBar().addPermanentWidget(self.connection_label)
        # Kill switch: stored in the database, so the logger (and the command line) see it too.
        self.gate = Gatekeeper(self.store)
        self.tx_button = QPushButton()
        self.tx_button.setFlat(True)
        self.tx_button.clicked.connect(self._toggle_transmit)
        self.statusBar().addPermanentWidget(self.tx_button)
        self._show_transmit()
        self.dataChanged.connect(self._show_transmit)

        self._data_version = None
        self._timer(DATA_POLL_MS, self._poll_data)
        self._timer(STATUS_POLL_MS, self.poll_status)
        self._timer(CLOCK_TICK_MS, self.dataChanged.emit)
        self.dataChanged.connect(self.activity.check)

        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1100, 750)
        QTimer.singleShot(0, self._poll_data)
        QTimer.singleShot(0, self.poll_status)

    def _timer(self, ms, fn):
        timer = QTimer(self)
        timer.timeout.connect(fn)
        timer.start(ms)
        return timer

    # ---- polling ----

    def _poll_data(self):
        version = self.store.data_version()
        if version != self._data_version:
            self._data_version = version
            self.dataChanged.emit()

    def poll_status(self):
        def done(result, error):
            status = result if error is None else {"connected": False, "hub_error": str(error)}
            if status != self.status:
                self.status = status
                self.statusChanged.emit(status)
            self._show_connection()

        self.hub.get("/api/status", done)

    def _show_connection(self):
        s = self.status
        if s.get("hub_error"):
            text = f"○ {s['hub_error']}"
        elif not s.get("connected"):
            text = "○ Logger running, radio not connected"
        else:
            node, metrics = s["node"], s.get("metrics", {})
            text = f"● {node.get('long_name') or node.get('id')} ({node.get('id')}) on {s.get('port')}"
            if "batteryLevel" in metrics:
                text += f" · battery {metrics['batteryLevel']}%"
            if "channelUtilization" in metrics:
                text += f" · channel util {metrics['channelUtilization']:.1f}%"
        self.connection_label.setText(text)

    def _show_automation_notifications(self):
        new = [r for r in self.store.automation_runs(limit=20) if r["id"] > self._last_notified]
        if not new:
            return
        self._last_notified = max(r["id"] for r in new)
        for run in reversed([r for r in new if r["status"] == "notified"]):
            if self.tray is not None:
                self.tray.showMessage(f"MeshShack: {run['job_name']}", run["text"] or "", app_icon(), 15000)
            self.toast(f"{run['job_name']}: {run['text']}", 15000)

    def _show_transmit(self):
        on = self.gate.transmit_enabled()
        self.tx_button.setText("● Transmit on" if on else "■ Transmit OFF")
        self.tx_button.setToolTip("Click to stop all transmissions immediately" if on else
                                  "Transmitting is off: nothing is sent, including config changes. Click to turn on.")
        self.tx_button.setStyleSheet("" if on else "color: white; background: #c5221f; font-weight: bold; padding: 2px 8px")

    def _toggle_transmit(self):
        if self.gate.transmit_enabled():
            self.gate.set_transmit_enabled(False, by="app")
            self.toast("Transmitting is off. Nothing will be sent until you turn it back on.")
        elif QMessageBox.question(self, "Transmit", "Turn transmitting back on?",
                                  QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            self.gate.set_transmit_enabled(True, by="app")
        self._show_transmit()
        if self.tray is not None:
            self.tray.refresh()

    def show_result(self, done_text, result):
        """Toast after a send; the gatekeeper's warning (e.g. a busy channel) takes precedence."""
        warning = (result or {}).get("warning")
        self.toast(f"{done_text}. Note: {warning}" if warning else done_text, 10000 if warning else 6000)

    @property
    def my_num(self):
        return (self.status.get("node") or {}).get("num")

    @property
    def radio_ready(self):
        return bool(self.status.get("connected"))

    # ---- actions shared by tabs ----

    def toast(self, text, ms=6000):
        self.statusBar().showMessage(text, ms)

    def open_dm(self, num):
        self.chat.open_dm(num)
        self.tabs.setCurrentWidget(self.chat)

    def send_request(self, num, kind):
        """kind: traceroute, position, telemetry or nodeinfo. Results show in the Nodes tab."""
        if kind == "traceroute":
            path, body = "/api/traceroute", {"to": num}
        else:
            path, body = "/api/request", {"to": num, "what": kind}

        def done(result, error):
            if error:
                self.activity.fail(kind, num, error)
                self.toast(f"{KIND_LABELS[kind]} failed: {error}")
            else:
                self.activity.add(result["packet_id"], kind, num)
                self.show_result(f"{KIND_LABELS[kind]} sent; the result will show in the Nodes tab", result)

        self.hub.post(path, body, done)

    def _show_unread(self, count):
        self.tabs.setTabText(self.tabs.indexOf(self.chat), f"Chat ({count})" if count else "Chat")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.chat.refresh()  # mark the open conversation read

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        if self.tray is not None and not self.quitting:
            # Closing the window just hides it; the tray keeps the app (and notifications) going.
            event.ignore()
            self.hide()
            if not self.settings.value("tray/hint_shown", False, type=bool):
                self.tray.showMessage("MeshShack", "Still running in the tray. The logger keeps recording "
                                      "either way; use the tray menu to quit the app.", app_icon(), 8000)
                self.settings.setValue("tray/hint_shown", True)
            return
        self.hub.shutdown()
        super().closeEvent(event)

    def quit_app(self):
        log.info("Quit from the tray")
        self.quitting = True
        self.close()
        QApplication.instance().quit()


INSTANCE_KEY = "meshshack-gui"


def _signal_running_instance():
    """If the app is already open, ask it to show its window. True if one answered."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


LOG_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "meshshack"
log = logging.getLogger("meshshack.gui")


def setup_logging():
    """Send everything the app says to ~/.local/state/meshshack/gui.log.

    A tray app outlives whatever started it, so its stdout/stderr may point at a terminal or pipe
    that's gone; writing there can end the process with no trace. When not attached to a terminal,
    point both at the log (this also catches the web engine's helper processes), and log Qt
    warnings, uncaught Python errors, and a crash's stack trace there too."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "gui.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger("meshshack")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # the CLI's stderr handler would write every line to the log twice
    if not sys.stderr.isatty():
        stream = open(path, "a", buffering=1)
        os.dup2(stream.fileno(), 1)
        os.dup2(stream.fileno(), 2)
    faulthandler.enable(open(LOG_DIR / "gui-crash.log", "a"), all_threads=True)

    def excepthook(kind, value, tb):  # keep running; a bug in one handler shouldn't end the app
        log.error("Uncaught exception", exc_info=(kind, value, tb))

    sys.excepthook = excepthook
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}
    qInstallMessageHandler(lambda kind, _ctx, message: log.log(levels.get(kind, logging.WARNING), "Qt: %s", message))
    log.info("MeshShack app starting (pid %d)", os.getpid())


def run_gui(db_path, start_hidden=False):
    """start_hidden: go straight to the tray (used at login); needs a desktop with a tray."""
    setup_logging()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("meshshack")
    app.setApplicationDisplayName("MeshShack")
    app.setDesktopFileName("meshshack")
    app.setWindowIcon(app_icon())
    if _signal_running_instance():
        log.info("Already running; asked it to show its window")
        return 0  # the running app shows its window instead

    window = MainWindow(Path(db_path))
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.tray = Tray(window, window.quit_app)
        window.tray.show()
        app.setQuitOnLastWindowClosed(False)
    else:
        start_hidden = False  # nowhere to hide to

    server = QLocalServer(app)
    QLocalServer.removeServer(INSTANCE_KEY)  # a stale socket left by a crash
    server.listen(INSTANCE_KEY)

    def show_window():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(conn.readAll)
            conn.disconnected.connect(conn.deleteLater)
        if window.tray is not None:
            window.tray.show_window()
        else:
            window.showNormal()
            window.raise_()
            window.activateWindow()

    server.newConnection.connect(show_window)
    if not start_hidden:
        window.show()
    code = app.exec()
    log.info("MeshShack app exiting (code %d)", code)
    return code
