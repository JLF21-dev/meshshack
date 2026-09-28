"""Turns traceroutes and node requests (and their replies, as logged by the hub) into readable results."""

import json
import time

from PySide6.QtCore import QObject, Signal

from ..store import REQUEST_TIMEOUT as TIMEOUT_SECONDS
from .common import fmt_clock, node_name

UNKNOWN_SNR = -128
UNKNOWN_NODE = 0xFFFFFFFF

KIND_LABELS = {
    "traceroute": "Traceroute",
    "position": "Position request",
    "telemetry": "Telemetry request",
    "nodeinfo": "Node info request",
}


class ActivityTracker(QObject):
    """The Nodes tab's activity list. Requests live in the hub's database (the logger matches
    replies even while the app is closed); sends that never reached the hub are kept here."""

    updated = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._not_sent = []  # (timestamp, text) for sends the hub refused or never got
        self._rows = []
        self._signature = None
        self.check()

    def add(self, packet_id, kind, num):
        """The hub accepted a request (and recorded it); show it right away."""
        self.check()

    def fail(self, kind, num, error):
        self._not_sent.insert(0, (time.time(), f"{KIND_LABELS[kind]} to {self._name(num)} not sent: {error}"))
        del self._not_sent[50:]
        self._signature = None
        self.check()

    def check(self):
        """Re-read requests; emits updated when anything visible changed. Called on every data change."""
        self._rows = self.store.requests()
        now = time.time()
        signature = (len(self._not_sent),
                     tuple((r["id"], r["status"], self._timed_out(r, now)) for r in self._rows))
        if signature != self._signature:
            self._signature = signature
            self.updated.emit()

    @staticmethod
    def _timed_out(row, now):
        return row["status"] == "pending" and now - row["sent_at"] > TIMEOUT_SECONDS

    @property
    def entries(self):
        """[(timestamp, text)], newest first."""
        now = time.time()
        entries = [(r["completed_at"] or r["sent_at"], self._text(r, now)) for r in self._rows]
        return sorted(entries + self._not_sent, key=lambda e: e[0], reverse=True)

    def lines(self):
        return [f"{fmt_clock(ts)}  {text}" for ts, text in self.entries]

    def _text(self, row, now):
        label = f"{KIND_LABELS.get(row['kind'], row['kind'])} to {self._name(row['to_num'])}"
        if row["response_json"]:
            return self._describe(row, json.loads(row["response_json"]))
        if self._timed_out(row, now):
            return f"{label}: no reply after {TIMEOUT_SECONDS // 60} minutes"
        return f"{label} sent…"

    def _name(self, num):
        if num == UNKNOWN_NODE:
            return "?"
        return node_name(self.store.node(num), num)

    def _describe(self, req, packet):
        label = f"{KIND_LABELS.get(req['kind'], req['kind'])} to {self._name(req['to_num'])}"
        decoded = packet.get("decoded") or {}
        if decoded.get("portnum") == "ROUTING_APP":
            reason = (decoded.get("routing") or {}).get("errorReason", "unknown error")
            return f"{label} failed: {reason}"

        if "traceroute" in decoded:
            tr = decoded["traceroute"]
            me = packet.get("to")
            towards = self._route([me, *tr.get("route", []), req["to_num"]], tr.get("snrTowards", []))
            text = f"{label}: {towards}"
            if "snrBack" in tr:
                back = self._route([req["to_num"], *tr.get("routeBack", []), me], tr.get("snrBack", []))
                text += f"  |  back: {back}"
            return text

        name = self._name(packet.get("from"))
        if "position" in decoded:
            pos = decoded["position"]
            if "latitude" in pos:
                return f"{label}: {name} is at {pos['latitude']:.5f}, {pos['longitude']:.5f}"
            return f"{label}: {name} replied without a position"
        if "telemetry" in decoded:
            m = decoded["telemetry"].get("deviceMetrics", {})
            parts = []
            if "batteryLevel" in m:
                parts.append(f"battery {m['batteryLevel']}%")
            if "voltage" in m:
                parts.append(f"{m['voltage']:.2f} V")
            if "channelUtilization" in m:
                parts.append(f"channel util {m['channelUtilization']:.1f}%")
            if "uptimeSeconds" in m:
                parts.append(f"up {m['uptimeSeconds'] // 3600}h")
            return f"{label}: {name} " + (", ".join(parts) or "replied")
        if "user" in decoded:
            u = decoded["user"]
            return f"{label}: {u.get('longName', '?')} ({u.get('shortName', '?')}, {u.get('hwModel', '?')})"
        return f"{label}: reply from {name}"

    def _route(self, nums, snrs):
        """'N0C → ALB (6.0 dB) → BOB (-3.2 dB)'; snrs[i] is how hop i+1 heard hop i, in quarter-dB."""
        parts = [self._name(nums[0])]
        for i, num in enumerate(nums[1:]):
            snr = snrs[i] if i < len(snrs) else UNKNOWN_SNR
            hop = self._name(num)
            parts.append(hop if snr == UNKNOWN_SNR else f"{hop} ({snr / 4:.1f} dB)")
        return " → ".join(parts)
