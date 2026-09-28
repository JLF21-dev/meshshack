"""Airtime gatekeeper: every transmission the hub makes is authorized (or refused) here first.

The mesh is a shared network, and anything sent can be repeated by many other nodes, so the
rules are deliberately conservative. They follow community practice (see README, "Airtime"):

- Kill switch: when transmitting is off, nothing is sent, including config writes (which
  reboot the radio, and a rebooting radio re-announces itself on the air).
- Costs reflect how much airtime a send spends across the mesh: a direct message or a
  single request is 1 credit; anything flooded (a channel broadcast, a traceroute, a node
  announcement, a config write that reboots the radio) is 3.
- You, in the app ("manual"): at most 5 sends a minute; traceroutes 3 minutes apart and
  at most 6 an hour. A busy channel gives a warning, not a refusal.
- Other apps ("api:<name>"): 6 credits an hour each, no broadcasts unless allowed, and the
  same node traced at most once every 3 hours.
- Automations ("automation:<job>"): each job at most once every 6 hours, 4 automated sends
  a day in total.
- Anything not manual: 30 seconds between sends, and paused while channel utilization is
  over 20% or this radio's own transmit airtime is over 5%.
- Local settings (favorite/ignore a node) aren't transmissions: logged at no cost and allowed
  with transmitting off, but only from the app.

Every decision, allowed or refused, is written to the tx_log table.
"""

import time
from dataclasses import dataclass

# "setting": a change stored on this radio only (favorite/ignore a node). Nothing is transmitted and
# the radio doesn't reboot, so it's logged but free, and allowed even with transmitting off.
COST = {"dm": 1, "request": 1, "broadcast": 3, "traceroute": 3, "announce": 3, "config": 3, "setting": 0}

MANUAL_PER_MINUTE = 5
TRACEROUTE_SPACING = 180  # any source; the firmware allows 30 s, the Meshtastic devs asked apps for 3 min
MANUAL_TRACEROUTES_PER_HOUR = 6
MANUAL_BUSY_WARNING = 25.0  # channel utilization %, where the firmware itself starts holding back

API_CREDITS_PER_HOUR = 6
SAME_NODE_TRACEROUTE_SPACING = 3 * 3600  # other apps and automations

AUTOMATION_JOB_SPACING = 6 * 3600
AUTOMATION_PER_DAY = 4

UNATTENDED_SPACING = 30  # between any two sends that aren't manual
BACKOFF_CHANNEL_UTIL = 20.0  # %
BACKOFF_AIR_UTIL_TX = 5.0  # %


class Refused(Exception):
    """The gatekeeper said no; the message says why, in words meant for the user."""

    def __init__(self, reason, status=429):
        super().__init__(reason)
        self.status = status


@dataclass
class Ticket:
    row: int
    warning: str | None = None


def source_class(source):
    return source.split(":", 1)[0]


class Gatekeeper:
    def __init__(self, store, metrics=lambda: {}, clock=time.time):
        """metrics: returns this radio's latest deviceMetrics (channelUtilization, airUtilTx)."""
        self.store = store
        self.metrics = metrics
        self.clock = clock

    # ---- kill switch ----

    def transmit_enabled(self):
        return self.store.station("transmit_enabled") is not False

    def set_transmit_enabled(self, enabled, by="manual"):
        self.store.set_station("transmit_enabled", bool(enabled))
        self.store.record_event("transmit", f"{'on' if enabled else 'off'} ({by})")

    # ---- decisions ----

    def authorize(self, source, kind, to=None, channel=None, allow_broadcast=False):
        """Returns a Ticket (the send is logged as allowed) or raises Refused (logged as refused).
        The caller then sends and reports the packet id with sent()."""
        now = self.clock()
        cost = COST[kind]
        try:
            warning = self._check(source, kind, to, now, allow_broadcast)
        except Refused as ex:
            self.store.record_tx(now, source, kind, to, channel, cost, False, str(ex))
            raise
        row = self.store.record_tx(now, source, kind, to, channel, cost, True, warning)
        return Ticket(row, warning)

    def sent(self, ticket, packet_id):
        self.store.set_tx_packet(ticket.row, packet_id)

    def _check(self, source, kind, to, now, allow_broadcast):
        if kind == "setting":
            if source_class(source) != "manual":
                raise Refused("Only you, in the app, can change the radio's settings", status=403)
            return None
        if not self.transmit_enabled():
            raise Refused("Transmitting is turned off (kill switch)", status=403)
        cls = source_class(source)
        if cls not in ("manual", "api", "automation"):
            raise Refused(f"unknown source {source!r}", status=400)
        log = self.store.tx_allowed_since(now - 86400)  # newest first

        if kind == "traceroute":
            last = next((r for r in log if r["kind"] == "traceroute"), None)
            if last and now - last["at"] < TRACEROUTE_SPACING:
                wait = int(TRACEROUTE_SPACING - (now - last["at"]))
                raise Refused(f"Traceroutes are limited to one every {TRACEROUTE_SPACING // 60} minutes; "
                              f"try again in {wait} s")

        if cls == "manual":
            recent = [r for r in log if source_class(r["source"]) == "manual" and now - r["at"] < 60]
            if len(recent) >= MANUAL_PER_MINUTE:
                raise Refused(f"More than {MANUAL_PER_MINUTE} sends in a minute; wait a moment")
            if kind == "traceroute":
                hour = [r for r in log if r["kind"] == "traceroute" and source_class(r["source"]) == "manual"
                        and now - r["at"] < 3600]
                if len(hour) >= MANUAL_TRACEROUTES_PER_HOUR:
                    raise Refused(f"At most {MANUAL_TRACEROUTES_PER_HOUR} traceroutes an hour")
            util = self.metrics().get("channelUtilization")
            if util is not None and util > MANUAL_BUSY_WARNING:
                return f"The channel is busy ({util:.0f}% utilization); sent anyway"
            return None

        # Unattended: other apps and automations.
        if kind in ("broadcast", "announce") and not allow_broadcast:
            raise Refused("Broadcasts aren't allowed for this sender", status=403)
        if kind == "config":
            raise Refused("Only you, in the app, can change the radio's configuration", status=403)
        last_unattended = next((r for r in log if source_class(r["source"]) != "manual"), None)
        if last_unattended and now - last_unattended["at"] < UNATTENDED_SPACING:
            raise Refused(f"Automated sends are spaced {UNATTENDED_SPACING} s apart; try again shortly")
        metrics = self.metrics()
        util, air = metrics.get("channelUtilization"), metrics.get("airUtilTx")
        if util is not None and util > BACKOFF_CHANNEL_UTIL:
            raise Refused(f"Paused: the channel is busy ({util:.0f}% utilization, limit {BACKOFF_CHANNEL_UTIL:.0f}%)")
        if air is not None and air > BACKOFF_AIR_UTIL_TX:
            raise Refused(f"Paused: this radio has been transmitting a lot ({air:.1f}% airtime, "
                          f"limit {BACKOFF_AIR_UTIL_TX:.0f}%)")
        if kind == "traceroute" and to is not None:
            same = next((r for r in log if r["kind"] == "traceroute" and r["to_num"] == to
                         and source_class(r["source"]) != "manual"), None)
            if same and now - same["at"] < SAME_NODE_TRACEROUTE_SPACING:
                raise Refused("That node was traced in the last 3 hours")

        if cls == "api":
            hour = sum(r["cost"] for r in log if r["source"] == source and now - r["at"] < 3600)
            if hour + COST[kind] > API_CREDITS_PER_HOUR:
                raise Refused(f"Hourly airtime budget used ({hour}/{API_CREDITS_PER_HOUR} credits)")
        else:  # automation
            same_job = next((r for r in log if r["source"] == source), None)
            if same_job and now - same_job["at"] < AUTOMATION_JOB_SPACING:
                raise Refused("This automation already sent in the last 6 hours")
            today = [r for r in log if source_class(r["source"]) == "automation"]
            if len(today) >= AUTOMATION_PER_DAY:
                raise Refused(f"The daily limit of {AUTOMATION_PER_DAY} automated sends is used up")
        return None
