"""Per-node history charts: small multiples, one measure per chart (never two y-axes).

Styling follows a validated reference palette: a single series in categorical slot 1 (blue,
stepped separately for light and dark), 1px solid hairline gridlines one step off the chart
surface, muted axis text, 2px lines with 8px markers when readings are sparse. Hovering shows a
crosshair that snaps to the nearest reading, with its time and value. A table view shows the
same numbers without a chart.
"""

import bisect
import math
import time

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QDateTime, QMargins, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsLineItem, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
    QStackedWidget, QTableWidget, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget,
)

LIGHT = {"surface": "#fcfcfb", "series": "#2a78d6", "grid": "#e1e0d9", "axis": "#c3c2b7",
         "muted": "#898781", "text": "#0b0b0b", "reference": "#c3c2b7"}
DARK = {"surface": "#1a1a19", "series": "#3987e5", "grid": "#2c2c2a", "axis": "#383835",
        "muted": "#898781", "text": "#ffffff", "reference": "#52514e"}

# key: (title, unit, y floor/ceiling or None for auto, reference line (value, label) or None)
METRICS = {
    "batteryLevel": ("Battery", "%", (0, 100), None),
    "voltage": ("Voltage", "V", None, None),
    "channelUtilization": ("Channel utilization", "%", (0, None), (25, "25%: firmware holds back its own sends")),
    "airUtilTx": ("Transmit airtime", "%", (0, None), None),
    "directSnr": ("Direct SNR", "dB", None, None),
}
RANGES = [("Last 24 hours", 86400), ("Last 7 days", 7 * 86400), ("Last 30 days", 30 * 86400), ("All time", None)]
MAX_POINTS = 600  # longer series are averaged into this many time buckets
SPARSE = 48  # show point markers at or below this many readings
REFRESH_SECONDS = 30  # at most this often while the data keeps changing


def downsample(points, limit=MAX_POINTS):
    """Average into `limit` equal time buckets, keeping each bucket's mean time."""
    if len(points) <= limit:
        return points
    start, end = points[0][0], points[-1][0]
    width = (end - start) / limit or 1
    buckets = {}
    for t, v in points:
        key = min(int((t - start) / width), limit - 1)
        acc = buckets.setdefault(key, [0.0, 0.0, 0])
        acc[0] += t
        acc[1] += v
        acc[2] += 1
    return [(acc[0] / acc[2], acc[1] / acc[2]) for _, acc in sorted(buckets.items())]


def fmt_value(key, value):
    if key == "batteryLevel" and value > 100:
        return "external power"
    unit = METRICS[key][1]
    return f"{value:.2f} {unit}" if key == "voltage" else f"{value:.1f} {unit}" if unit != "%" else f"{value:.1f}%"


def is_dark(widget):
    return widget.palette().color(QPalette.Window).lightness() < 128


class MetricChart(QChartView):
    def __init__(self, key, points, span, colors):
        super().__init__()
        self.key = key
        self.points = points
        self.times = [t for t, _ in points]
        title, unit, bounds, reference = METRICS[key]
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(190)
        self.setMouseTracking(True)

        chart = QChart()
        chart.legend().hide()
        chart.setBackgroundBrush(QColor(colors["surface"]))
        chart.setBackgroundRoundness(4)
        chart.setMargins(QMargins(4, 4, 8, 4))
        chart.setTitle(f"{title} ({unit})")
        font = QFont()
        font.setBold(True)
        chart.setTitleFont(font)
        chart.setTitleBrush(QColor(colors["text"]))

        shown = [(t, min(v, 100.0) if key == "batteryLevel" else v) for t, v in points]
        line = QLineSeries()
        pen = QPen(QColor(colors["series"]))
        pen.setWidthF(2)
        line.setPen(pen)
        for t, v in shown:
            line.append(t * 1000, v)
        chart.addSeries(line)
        series = [line]
        if len(shown) <= SPARSE:
            dots = QScatterSeries()
            dots.setMarkerSize(8)
            dots.setColor(QColor(colors["series"]))
            dots.setBorderColor(QColor(colors["surface"]))
            for t, v in shown:
                dots.append(t * 1000, v)
            chart.addSeries(dots)
            series.append(dots)

        values = [v for _, v in shown]
        low, high = min(values), max(values)
        if bounds:
            low = bounds[0] if bounds[0] is not None else low
            high = bounds[1] if bounds[1] is not None else high
        if reference and high >= reference[0] * 0.6:  # only when the data gets anywhere near it
            high = max(high, reference[0] * 1.1)
            ref = QLineSeries()
            ref_pen = QPen(QColor(colors["reference"]))
            ref_pen.setWidthF(1)
            ref.setPen(ref_pen)
            ref.append(shown[0][0] * 1000, reference[0])
            ref.append(shown[-1][0] * 1000, reference[0])
            chart.addSeries(ref)
            series.append(ref)
            self.setToolTip(reference[1])
        pad = (high - low) * 0.08 or max(abs(high) * 0.1, 1)
        if not bounds or bounds[1] is None:
            high += pad
        if not bounds or bounds[0] is None:
            low -= pad

        x_axis = QDateTimeAxis()
        x_axis.setFormat("HH:mm" if span <= 86400 else "MMM d")
        x_axis.setTickCount(5)
        y_axis = QValueAxis()
        y_axis.setRange(low, high)
        y_axis.setTickCount(5)
        y_axis.applyNiceNumbers()  # round tick values (0, 10, 20, ...) instead of range/4 steps
        if bounds and bounds[0] is not None and y_axis.min() < bounds[0]:
            y_axis.setMin(bounds[0])
        # Enough decimals that neighboring ticks never print the same number.
        step = (y_axis.max() - y_axis.min()) / max(y_axis.tickCount() - 1, 1)
        y_axis.setLabelFormat(f"%.{max(0, -math.floor(math.log10(step))) if step > 0 else 0}f")
        for axis in (x_axis, y_axis):
            axis.setGridLineColor(QColor(colors["grid"]))
            axis.setLinePenColor(QColor(colors["axis"]))
            axis.setLabelsColor(QColor(colors["muted"]))
            grid_pen = axis.gridLinePen()
            grid_pen.setWidthF(1)
            grid_pen.setStyle(Qt.SolidLine)
            axis.setGridLinePen(grid_pen)
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        for s in series:
            s.attachAxis(x_axis)
            s.attachAxis(y_axis)
        if len(shown) == 1:  # a single reading: widen the time axis so it isn't a zero-width range
            t = shown[0][0]
            x_axis.setRange(QDateTime.fromSecsSinceEpoch(int(t - 1800)), QDateTime.fromSecsSinceEpoch(int(t + 1800)))
        self.setChart(chart)
        self.line_series = line

        self.crosshair = QGraphicsLineItem()
        cross_pen = QPen(QColor(colors["muted"]))
        cross_pen.setWidthF(1)
        self.crosshair.setPen(cross_pen)
        self.crosshair.hide()
        chart.scene().addItem(self.crosshair)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        chart = self.chart()
        area = chart.plotArea()
        scene_pos = self.mapToScene(event.position().toPoint())
        if not area.contains(scene_pos) or not self.times:
            self.crosshair.hide()
            QToolTip.hideText()
            return
        t = chart.mapToValue(chart.mapFromScene(scene_pos), self.line_series).x() / 1000
        i = bisect.bisect_left(self.times, t)
        i = min(range(max(0, i - 1), min(len(self.times), i + 1)), key=lambda j: abs(self.times[j] - t))
        when, value = self.points[i]
        x = chart.mapToScene(chart.mapToPosition(QPointF(when * 1000, 0), self.line_series)).x()
        self.crosshair.setLine(x, area.top(), x, area.bottom())
        self.crosshair.show()
        stamp = time.strftime("%b %-d %H:%M", time.localtime(when))
        QToolTip.showText(event.globalPosition().toPoint(), f"{stamp}  ·  {fmt_value(self.key, value)}", self)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.crosshair.hide()
        QToolTip.hideText()


class NodeCharts(QWidget):
    """Charts (or a table) of one node's history, with a time range picker above."""

    def __init__(self, store, empty_text="Select a node to see its history."):
        super().__init__()
        self.store = store
        self.num = None
        self.empty_text = empty_text
        self._drawn = None  # (num, range, table, dark) last drawn
        self._drawn_at = 0

        self.range_box = QComboBox()
        for label, seconds in RANGES:
            self.range_box.addItem(label, seconds)
        self.range_box.currentIndexChanged.connect(lambda _: self.refresh(force=True))
        self.table_toggle = QCheckBox("Show as table")
        self.table_toggle.toggled.connect(lambda _: self.refresh(force=True))
        self.note = QLabel()
        self.note.setStyleSheet("color: gray")
        top = QHBoxLayout()
        top.addWidget(QLabel("Range:"))
        top.addWidget(self.range_box)
        top.addWidget(self.table_toggle)
        top.addWidget(self.note, 1)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Time", "Measure", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.empty = QLabel(empty_text)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: gray")
        self.stack = QStackedWidget()
        for w in (self.empty, scroll, self.table):
            self.stack.addWidget(w)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.stack, 1)
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.timeout.connect(lambda: self.refresh(force=True))

    def set_node(self, num):
        if num != self.num:
            self.num = num
            self.refresh(force=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self, force=False):
        """Redraw if something changed; data changes are coalesced to one redraw per REFRESH_SECONDS."""
        if not self.isVisible():
            return
        key = (self.num, self.range_box.currentIndex(), self.table_toggle.isChecked(), is_dark(self))
        if not force and key == self._drawn:
            wait = REFRESH_SECONDS - (time.time() - self._drawn_at)
            if wait > 0:
                if not self._pending.isActive():
                    self._pending.start(int(wait * 1000))
                return
        self._drawn, self._drawn_at = key, time.time()
        self._draw()

    def _draw(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.num is None:
            self.empty.setText(self.empty_text)
            self.note.setText("")
            self.stack.setCurrentWidget(self.empty)
            return
        span = self.range_box.currentData()
        since = time.time() - span if span else 0
        series = {k: v for k, v in self.store.node_series(self.num, since).items() if v}
        if not series:
            self.empty.setText("Nothing charted for this node in this range yet: it hasn't sent device "
                               "telemetry, and hasn't been heard directly.")
            self.note.setText("")
            self.stack.setCurrentWidget(self.empty)
            return
        total = sum(len(v) for v in series.values())
        averaged = any(len(v) > MAX_POINTS for v in series.values())
        self.note.setText(f"{total} readings" + (" (long ranges are averaged)" if averaged else ""))
        if self.table_toggle.isChecked():
            rows = sorted(((t, k, v) for k, pts in series.items() for t, v in pts), reverse=True)
            self.table.setRowCount(len(rows))
            for r, (t, k, v) in enumerate(rows):
                for c, text in enumerate((time.strftime("%Y-%m-%d %H:%M", time.localtime(t)),
                                          METRICS[k][0], fmt_value(k, v))):
                    self.table.setItem(r, c, QTableWidgetItem(text))
            self.stack.setCurrentWidget(self.table)
            return
        colors = DARK if is_dark(self) else LIGHT
        span_actual = span or (max(p[-1][0] for p in series.values()) - min(p[0][0] for p in series.values()))
        for i, key in enumerate(k for k in METRICS if k in series):
            self.grid.addWidget(MetricChart(key, downsample(series[key]), span_actual, colors), i // 2, i % 2)
        self.stack.setCurrentIndex(1)
