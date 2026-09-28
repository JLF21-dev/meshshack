"""System tray icon: keeps the app running with the window closed, and shows state at a glance.

The logger (`meshshack run`, a systemd service) records whether or not this app is open. The
tray just makes the window one click away. The icon shows the unread count, turns red when
transmitting is switched off, and grey when the radio isn't connected.
"""

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

ICON_PATH = Path(__file__).parent / "assets" / "icon.svg"
SIZE = 64


def app_icon():
    return QIcon(str(ICON_PATH))


def state_icon(unread=0, transmit_on=True, connected=True):
    """The app icon with state drawn on top: grey when disconnected, a red bar when transmit
    is off, and a badge with the unread count."""
    pixmap = app_icon().pixmap(SIZE, SIZE)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    if not connected:
        painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        painter.fillRect(pixmap.rect(), QColor(128, 128, 128, 170))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    if not transmit_on:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d93025"))
        painter.drawRoundedRect(QRectF(4, SIZE - 18, SIZE - 8, 14), 5, 5)
    if unread:
        text = str(unread) if unread < 100 else "99+"
        diameter = 30 if len(text) < 3 else 36
        rect = QRectF(SIZE - diameter, 0, diameter, diameter)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d93025" if transmit_on else "#1a73e8"))
        painter.drawEllipse(rect)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(19 if len(text) == 1 else 15)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(rect, Qt.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    def __init__(self, win, quit_app):
        super().__init__(app_icon(), win)
        self.win = win
        self._last_unread = None  # unknown until the chat tab first reports; don't notify for old mail

        menu = QMenu()
        self.open_action = QAction("Open MeshShack", menu)
        self.open_action.triggered.connect(self.show_window)
        self.transmit_action = QAction("Transmit", menu)
        self.transmit_action.setCheckable(True)
        self.transmit_action.triggered.connect(lambda _checked: win._toggle_transmit())
        quit_action = QAction("Quit (the logger keeps recording)", menu)
        quit_action.triggered.connect(quit_app)
        menu.addAction(self.open_action)
        menu.addSeparator()
        menu.addAction(self.transmit_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.setContextMenu(menu)
        self._menu = menu  # the tray doesn't take ownership

        self.activated.connect(self._activated)
        win.chat.unreadChanged.connect(self._unread_changed)
        win.statusChanged.connect(lambda _: self.refresh())
        win.dataChanged.connect(self.refresh)
        self.refresh()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.win.isVisible() and self.win.isActiveWindow():
                self.win.hide()
            else:
                self.show_window()

    def show_window(self):
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def _unread_changed(self, count):
        # Only notify for new arrivals while you're not looking at the window.
        known = self._last_unread is not None
        if known and count > self._last_unread and not (self.win.isVisible() and self.win.isActiveWindow()):
            new = count - self._last_unread
            self.showMessage("MeshShack", f"{new} new message{'s' if new != 1 else ''}", app_icon(), 8000)
        self._last_unread = count
        self.refresh()

    def refresh(self):
        transmit_on = self.win.gate.transmit_enabled()
        connected = self.win.radio_ready
        unread = self._last_unread or 0
        self.setIcon(state_icon(unread, transmit_on, connected))
        self.transmit_action.setChecked(transmit_on)
        self.transmit_action.setText("Transmit on" if transmit_on else "Transmit OFF (click to turn on)")
        node = (self.win.status.get("node") or {}).get("long_name")
        lines = ["MeshShack",
                 f"{node} connected" if connected else "Radio not connected",
                 "Transmitting" if transmit_on else "Transmit is OFF"]
        if unread:
            lines.append(f"{unread} unread")
        self.setToolTip("\n".join(lines))
