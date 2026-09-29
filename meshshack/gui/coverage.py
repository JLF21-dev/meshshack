"""Coverage tab: how this station hears the mesh (see ../coverage.py). Nothing is transmitted.

Top: a summary and the time range. Middle: direct neighbors (distance, bearing, SNR spread and
link margin) beside where your traffic comes from (directly, through each relay, via MQTT).
Bottom: SNR against distance for direct neighbors with a position: one point per neighbor at its
median SNR, a vertical bar for its best-to-worst spread, a horizontal bar for its distance range
when its position is rounded, and a line at the preset's decoding limit.
"""

import time

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QDateTime, QMargins, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton,
    QSplitter, QStyledItemDelegate, QTabWidget, QTableWidget, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget,
)

from ..coverage import report, station_from_store
from .charts import DARK, LIGHT, RANGES, is_dark
from .common import fmt_ago, station_position

REFRESH_SECONDS = 30


def distance_text(n):
    if n["distance_range_km"] is None:
        return "—"
    near, far = n["distance_range_km"]
    return f"{near:.1f}–{far:.1f} km" if n["rounded"] else f"{n['distance_km']:.1f} km"


def name(n):
    return n["short_name"] or n["id"]


class ShareDelegate(QStyledItemDelegate):
    """Draws a cell's share (0..1, in UserRole) as a bar behind its text."""

    def __init__(self, color_fn, parent=None):
        super().__init__(parent)
        self.color_fn = color_fn

    def paint(self, painter, option, index):
        share = index.data(Qt.UserRole)
        if isinstance(share, float):
            rect = QRectF(option.rect).adjusted(2, 4, -2, -4)
            painter.save()
            painter.setPen(Qt.NoPen)
            color = QColor(self.color_fn())
            color.setAlpha(90)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect.left(), rect.top(), max(rect.width() * share, 2), rect.height()), 3, 3)
            painter.restore()
        super().paint(painter, option, index)


class SnrDistanceChart(QChartView):
    def __init__(self):
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(260)
        self.points = []  # (x_km, y_db, neighbor) for hover

    def update_chart(self, rep, colors):
        chart = QChart()
        chart.legend().hide()
        chart.setBackgroundBrush(QColor(colors["surface"]))
        chart.setBackgroundRoundness(4)
        chart.setMargins(QMargins(4, 4, 8, 4))
        chart.setTitle("Direct neighbors: SNR against distance")
        font = QFont()
        font.setBold(True)
        chart.setTitleFont(font)
        chart.setTitleBrush(QColor(colors["text"]))
        series_color = QColor(colors["series"])

        placed = [n for n in rep["neighbors"] if n["distance_range_km"] is not None]
        self.points = []
        dots = QScatterSeries()
        dots.setMarkerSize(10)
        dots.setColor(series_color)
        dots.setBorderColor(QColor(colors["surface"]))  # 2px surface ring
        bars = []
        pen = QPen(series_color)
        pen.setWidthF(2)
        for n in placed:
            x, y = n["distance_km"], n["snr_median"]
            dots.append(x, y)
            self.points.append((x, y, n))
            spread = QLineSeries()
            spread.setPen(pen)
            spread.append(x, n["snr_worst"])
            spread.append(x, n["snr_best"])
            bars.append(spread)
            if n["rounded"]:
                near, far = n["distance_range_km"]
                span = QLineSeries()
                faint = QPen(series_color)
                faint.setWidthF(1.5)
                faint.setStyle(Qt.DashLine)  # uncertainty, not a measurement
                span.setPen(faint)
                span.append(near, y)
                span.append(far, y)
                bars.append(span)
        for s in bars:
            chart.addSeries(s)
        chart.addSeries(dots)
        # Direct labels: each neighbor's name beside its point (there are only ever a handful).
        labels = []
        for n in placed:
            tag = QScatterSeries()
            tag.setMarkerSize(0.1)
            tag.setColor(QColor(0, 0, 0, 0))
            tag.setBorderColor(QColor(0, 0, 0, 0))
            # Qt centres a point label above its point, so anchor it a little to the right.
            x_span = max((v for m in placed for v in m["distance_range_km"]), default=1) * 1.1
            tag.append(n["distance_km"] + x_span * 0.035, n["snr_median"] - 2.2)
            tag.setPointLabelsFormat(name(n))
            tag.setPointLabelsVisible(True)
            tag.setPointLabelsColor(QColor(colors["text"]))
            tag.setPointLabelsClipping(False)
            labels.append(tag)
            chart.addSeries(tag)

        xs = [v for n in placed for v in n["distance_range_km"]] or [0, 1]
        ys = [v for n in placed for v in (n["snr_best"], n["snr_worst"])] or [-20, 10]
        limit = rep["snr_limit"]
        if limit is not None:
            ys.append(limit)
        x_axis, y_axis = QValueAxis(), QValueAxis()
        x_axis.setRange(0, max(xs) * 1.1 or 1)
        x_axis.setTitleText("distance (km)")
        y_axis.setRange(min(ys) - 2, max(ys) + 2)
        y_axis.setTitleText("SNR (dB)")
        for axis in (x_axis, y_axis):
            axis.applyNiceNumbers()
            axis.setLabelFormat("%.0f")
            axis.setGridLineColor(QColor(colors["grid"]))
            axis.setLinePenColor(QColor(colors["axis"]))
            axis.setLabelsColor(QColor(colors["muted"]))
            axis.setTitleBrush(QColor(colors["muted"]))
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        if limit is not None:
            ref = QLineSeries()
            ref_pen = QPen(QColor(colors["reference"]))
            ref_pen.setWidthF(1)
            ref.setPen(ref_pen)
            ref.append(x_axis.min(), limit)
            ref.append(x_axis.max(), limit)
            chart.addSeries(ref)
            ref_label = QScatterSeries()
            ref_label.setMarkerSize(0.1)
            ref_label.setColor(QColor(0, 0, 0, 0))
            ref_label.setBorderColor(QColor(0, 0, 0, 0))
            ref_label.append(x_axis.min() + (x_axis.max() - x_axis.min()) * 0.8, limit)
            ref_label.setPointLabelsFormat(f"decoding limit, about {limit:.1f} dB")
            ref_label.setPointLabelsVisible(True)
            ref_label.setPointLabelsColor(QColor(colors["muted"]))
            chart.addSeries(ref_label)
            self.setToolTip(f"The line is the decoding limit for this preset ({limit:.1f} dB SNR); "
                            "a link's margin is how far above it the link sits.")
        for s in chart.series():
            s.attachAxis(x_axis)
            s.attachAxis(y_axis)
        dots.hovered.connect(self._hovered)
        self.setChart(chart)

    def _hovered(self, point, state):
        if not state:
            QToolTip.hideText()
            return
        n = min(self.points, key=lambda p: (p[0] - point.x()) ** 2 + (p[1] - point.y()) ** 2)[2]
        margin = f", {n['margin_db']:+.1f} dB margin" if n["margin_db"] is not None else ""
        QToolTip.showText(QCursor.pos(),
                          f"{name(n)}: {distance_text(n)} {n['bearing'] or ''}\n"
                          f"SNR median {n['snr_median']:.1f} dB (best {n['snr_best']:.1f}, worst {n['snr_worst']:.1f}){margin}\n"
                          f"{n['packets']} packets heard directly", self)


# Categorical slots 1-3 of the reference palette (validated as a set, light and dark). A relay keeps
# its slot: the three shown are the all-time top relays, so a range change never repaints one.
RELAY_COLORS = {"light": ["#2a78d6", "#eb6834", "#1baf7a"], "dark": ["#3987e5", "#d95926", "#199e70"]}


def day_start(ts):
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class OverTimeChart(QChartView):
    """One measure over time: one or more series, markers on each bucket, and note lines."""

    def __init__(self, title, unit, series, notes, colors, series_colors, y_max=None, bucket=86400):
        """series: [(name, [(t, value)])]; notes: [{"at", "text"}]."""
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(220)
        chart = QChart()
        chart.setBackgroundBrush(QColor(colors["surface"]))
        chart.setBackgroundRoundness(4)
        chart.setMargins(QMargins(4, 4, 8, 4))
        chart.setTitle(f"{title} ({unit})" if unit else title)
        font = QFont()
        font.setBold(True)
        chart.setTitleFont(font)
        chart.setTitleBrush(QColor(colors["text"]))
        legend = chart.legend()
        legend.setVisible(len(series) > 1)  # one series: the title names it
        legend.setLabelColor(QColor(colors["text"]))
        legend.setAlignment(Qt.AlignBottom)
        x_axis, y_axis = QDateTimeAxis(), QValueAxis()
        all_t = [t for _, pts in series for t, _ in pts] or [time.time()]
        all_v = [v for _, pts in series for _, v in pts] or [0]
        x_axis.setFormat("MMM d" if bucket >= 86400 else "MMM d HH:mm")
        x_axis.setRange(QDateTime.fromSecsSinceEpoch(int(min(all_t) - 3600)),
                        QDateTime.fromSecsSinceEpoch(int(max(all_t) + 3600)))
        x_axis.setTickCount(5)
        y_axis.setRange(0, y_max if y_max is not None else max(max(all_v) * 1.15, 1))
        y_axis.applyNiceNumbers()
        y_axis.setLabelFormat("%.0f")
        for axis in (x_axis, y_axis):
            axis.setGridLineColor(QColor(colors["grid"]))
            axis.setLinePenColor(QColor(colors["axis"]))
            axis.setLabelsColor(QColor(colors["muted"]))
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        for (name, pts), color in zip(series, series_colors):
            line, dots = QLineSeries(), QScatterSeries()
            pen = QPen(QColor(color))
            pen.setWidthF(2)
            line.setPen(pen)
            line.setName(name)
            dots.setMarkerSize(8)
            dots.setColor(QColor(color))
            dots.setBorderColor(QColor(colors["surface"]))
            for t, v in pts:
                line.append(t * 1000, v)
                dots.append(t * 1000, v)
            for s in (line, dots):
                chart.addSeries(s)
                s.attachAxis(x_axis)
                s.attachAxis(y_axis)
            chart.legend().markers(dots)[0].setVisible(False)
        for note in notes:  # a labeled vertical line at each note
            mark = QLineSeries()
            mark_pen = QPen(QColor(colors["muted"]))
            mark_pen.setWidthF(1)
            mark_pen.setStyle(Qt.DashLine)
            mark.setPen(mark_pen)
            mark.append(note["at"] * 1000, y_axis.min())
            mark.append(note["at"] * 1000, y_axis.max())
            mark.setPointLabelsFormat(note["text"])
            chart.addSeries(mark)
            mark.attachAxis(x_axis)
            mark.attachAxis(y_axis)
            chart.legend().markers(mark)[0].setVisible(False)
            label = QScatterSeries()
            label.setMarkerSize(0.1)
            label.setColor(QColor(0, 0, 0, 0))
            label.setBorderColor(QColor(0, 0, 0, 0))
            label.append(note["at"] * 1000, y_axis.max() * 0.93)
            label.setPointLabelsFormat(note["text"])
            label.setPointLabelsVisible(True)
            label.setPointLabelsColor(QColor(colors["muted"]))
            label.setPointLabelsClipping(False)
            chart.addSeries(label)
            label.attachAxis(x_axis)
            label.attachAxis(y_axis)
            chart.legend().markers(label)[0].setVisible(False)
        self.setChart(chart)


class OverTimeView(QWidget):
    """Coverage per day (or per 6 hours for short ranges), with notes like "antenna moved"."""

    def __init__(self, tab):
        super().__init__()
        self.tab = tab
        self.store = tab.store
        add = QPushButton("Add note…")
        add.setToolTip("Mark a change, e.g. \"antenna moved to the roof\", to compare before and after")
        add.clicked.connect(self._add_note)
        remove = QPushButton("Remove a note…")
        remove.clicked.connect(self._remove_note)
        top = QHBoxLayout()
        top.addWidget(add)
        top.addWidget(remove)
        top.addStretch(1)
        self.grid = QGridLayout()
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(self.grid, 1)

    def notes(self):
        return self.store.station("coverage_notes") or []

    def _add_note(self):
        text, ok = QInputDialog.getText(self, "Coverage note", "What changed now? (e.g. antenna moved to the roof)")
        if ok and text.strip():
            self.store.set_station("coverage_notes", self.notes() + [{"at": time.time(), "text": text.strip()[:40]}])
            self.tab.refresh(force=True)

    def _remove_note(self):
        notes = self.notes()
        if not notes:
            return
        labels = [f"{time.strftime('%b %-d %H:%M', time.localtime(n['at']))}: {n['text']}" for n in notes]
        choice, ok = QInputDialog.getItem(self, "Remove note", "Note", labels, 0, False)
        if ok:
            self.store.set_station("coverage_notes", [n for n, l in zip(notes, labels) if l != choice])
            self.tab.refresh(force=True)

    def update_view(self, span, colors, dark):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                # Detach now (Python then frees it); deleteLater alone would leave it until the next event loop.
                item.widget().setParent(None)
        now = time.time()
        first = self.store._query("SELECT MIN(logged_at) AS t FROM packets WHERE is_local = 0")[0]["t"] or now
        since = max(now - span, first) if span else first
        bucket = 6 * 3600 if now - since <= 3 * 86400 else 86400
        since = day_start(since)
        rows = self.store.coverage_over_time(since, bucket)
        if not rows:
            self.grid.addWidget(QLabel("Nothing logged in this range yet."), 0, 0)
            return
        mid = [(r["start"] + bucket / 2, r) for r in rows]
        direct = [(t, 100 * r["direct"] / r["heard"]) for t, r in mid if r["heard"]]
        neighbors = [(t, r["neighbors"]) for t, r in mid]
        top = self.store.coverage(0)["relays"]
        top = [int(b) for b, _ in sorted(top.items(), key=lambda kv: -kv[1])[:3]]
        nodes = {n["num"]: n for n in self.store.nodes()}
        direct_nums = set(self.store.coverage(0)["direct"])
        from ..coverage import relay_candidates
        relay_series = []
        for byte in top:
            cands = relay_candidates(byte, direct_nums, nodes)
            name = (nodes[cands[0]]["short_name"] or f"!{cands[0]:08x}") if len(cands) == 1 else f"ID ends 0x{byte:02x}"
            relay_series.append((f"via {name}", [(t, 100 * r["relays"].get(byte, 0) / r["heard"]) for t, r in mid if r["heard"]]))
        notes = [n for n in self.notes() if n["at"] >= since]
        palette = RELAY_COLORS["dark" if dark else "light"]
        per = "per 6 hours" if bucket < 86400 else "per day"
        self.grid.addWidget(OverTimeChart(f"Heard directly, {per}", "% of packets", [("heard directly", direct)], notes,
                                          colors, [colors["series"]], y_max=max(10, max(v for _, v in direct) * 1.3),
                                          bucket=bucket), 0, 0)
        self.grid.addWidget(OverTimeChart(f"Nodes heard directly, {per}", "", [("nodes", neighbors)], notes, colors,
                                          [colors["series"]], bucket=bucket), 0, 1)
        if relay_series:
            self.grid.addWidget(OverTimeChart(f"Share of packets through each top relay, {per}", "%", relay_series,
                                              notes, colors, palette, y_max=100, bucket=bucket), 1, 0, 1, 2)


class CoverageTab(QWidget):
    NEIGHBOR_COLUMNS = ["Node", "Distance", "Bearing", "Packets", "Median SNR", "Best", "Worst", "Margin",
                        "Last direct"]
    SOURCE_COLUMNS = ["Where your traffic comes from", "Packets", "Share"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.store = win.store
        self.rep = None
        self._drawn_at = 0
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.timeout.connect(lambda: self.refresh(force=True))

        self.range_box = QComboBox()
        for label, seconds in RANGES:
            self.range_box.addItem(label, seconds)
        self.range_box.setCurrentIndex(int(win.settings.value("coverage/range", 1)))
        self.range_box.currentIndexChanged.connect(self._range_changed)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        top = QHBoxLayout()
        top.addWidget(QLabel("Range:"))
        top.addWidget(self.range_box)
        top.addStretch(1)

        self.neighbors = self._table(self.NEIGHBOR_COLUMNS)
        self.neighbors.horizontalHeaderItem(self.NEIGHBOR_COLUMNS.index("Margin")).setToolTip(
            "Median SNR above the lowest SNR this modem preset can decode. A few dB is a fragile link; "
            "10 dB or more is solid.")
        self.neighbors.horizontalHeaderItem(self.NEIGHBOR_COLUMNS.index("Distance")).setToolTip(
            "A range when the node shares a rounded position: the nearest and farthest it could be.")
        self.sources = self._table(self.SOURCE_COLUMNS)
        self.sources.setItemDelegateForColumn(2, ShareDelegate(self._series_color, self.sources))
        self.sources.horizontalHeaderItem(0).setToolTip(
            "A relay is known only by the last byte of its ID, matched to nodes you hear directly; "
            "'?' means the match is a guess.")
        self.chart = SnrDistanceChart()

        tables = QSplitter()
        for title, table in (("Heard directly", self.neighbors), ("Traffic sources", self.sources)):
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.addWidget(QLabel(f"<b>{title}</b>"))
            box_layout.addWidget(table)
            tables.addWidget(box)
        tables.setSizes([640, 420])
        self.over_time = OverTimeView(self)
        self.views = QTabWidget()
        self.views.addTab(self.chart, "SNR against distance")
        self.views.addTab(self.over_time, "Over time")
        self.views.setCurrentIndex(int(win.settings.value("coverage/view", 0)))
        self.views.currentChanged.connect(lambda i: (win.settings.setValue("coverage/view", i), self.refresh(force=True)))
        body = QSplitter(Qt.Vertical)
        body.addWidget(tables)
        body.addWidget(self.views)
        body.setSizes([280, 420])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(body, 1)
        win.dataChanged.connect(self.refresh)
        win.statusChanged.connect(lambda _: self.refresh(force=True))

    @staticmethod
    def _table(columns):
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _series_color(self):
        return (DARK if is_dark(self) else LIGHT)["series"]

    def _range_changed(self, index):
        self.win.settings.setValue("coverage/range", index)
        self.refresh(force=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(force=True)

    def refresh(self, force=False):
        """Recompute at most every REFRESH_SECONDS while data keeps arriving."""
        if not self.isVisible():
            return
        wait = REFRESH_SECONDS - (time.time() - self._drawn_at)
        if not force and wait > 0:
            if not self._pending.isActive():
                self._pending.start(int(wait * 1000))
            return
        self._drawn_at = time.time()
        span = self.range_box.currentData()
        station = station_position(self.win.status, self.store, self.win.my_num)
        if station[0] is None:  # radio not connected: use what the log knows (analysis works offline)
            station = station_from_store(self.store)
        preset = (self.win.status.get("lora") or {}).get("modem_preset")
        self.rep = report(self.store, station, preset, since=time.time() - span if span else 0)
        self._fill(station)

    def _fill(self, station):
        rep = self.rep
        t = rep["totals"]
        neighbors = rep["neighbors"]
        placed = [n for n in neighbors if n["distance_range_km"] is not None]
        lines = [f"Heard <b>{t['heard']}</b> packets; <b>{len(neighbors)}</b> node{'s' if len(neighbors) != 1 else ''} "
                 f"heard directly ({t['direct'] / (t['heard'] or 1):.0%} of packets)."]
        relays = [s for s in rep["sources"] if s["kind"] == "relay"]
        if relays:
            top = relays[0]
            lines.append(f"Most traffic reaches you through relays; the busiest is {top['label'][4:]}, "
                         f"with {top['share']:.0%}.")
        if placed:
            far = max(placed, key=lambda n: n["distance_range_km"][1])
            lines.append(f"Farthest direct link: {name(far)}, {distance_text(far)} {far['bearing']}.")
        if station[0] is None:
            lines.append("Set this station's position (Device tab) to see distances and bearings.")
        if rep["snr_limit"] is None:
            lines.append("Link margins need the radio's modem preset: connect the radio.")
        self.summary.setText(" ".join(lines))

        self.neighbors.setRowCount(len(neighbors))
        for r, n in enumerate(neighbors):
            cells = [f"{name(n)}" + (f"  {n['long_name']}" if n["long_name"] else ""), distance_text(n),
                     n["bearing"] or "—", str(n["packets"]), f"{n['snr_median']:.1f} dB", f"{n['snr_best']:.1f}",
                     f"{n['snr_worst']:.1f}", f"{n['margin_db']:+.1f} dB" if n["margin_db"] is not None else "—",
                     fmt_ago(n["last_direct"])]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.neighbors.setItem(r, c, item)

        sources = rep["sources"]
        self.sources.setRowCount(len(sources))
        for r, s in enumerate(sources):
            share = QTableWidgetItem(f"{s['share']:.1%}")
            share.setData(Qt.UserRole, s["share"])
            share.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            count = QTableWidgetItem(str(s["packets"]))
            count.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, item in enumerate((QTableWidgetItem(s["label"]), count, share)):
                self.sources.setItem(r, c, item)

        self.chart.update_chart(rep, DARK if is_dark(self) else LIGHT)
        if self.views.currentWidget() is self.over_time:
            self.over_time.update_view(self.range_box.currentData(), DARK if is_dark(self) else LIGHT, is_dark(self))
