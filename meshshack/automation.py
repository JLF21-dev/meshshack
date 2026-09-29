"""Automation: your own messages, sent on a schedule, with content filled in from data sources.

This is the one part of MeshShack that transmits on its own, so it's built around the airtime
rules (see README, "Airtime"):
- Every send goes through the gatekeeper as "automation:<job name>": each job at most every 6
  hours, 4 automated sends a day in total, 30 s apart, paused while the channel is busy, and
  never while transmitting is switched off.
- New jobs start in dry-run mode: they record what they would have sent, and send nothing.
- A message is never truncated or sent half-filled: if it renders over 200 bytes, or a data
  source fails, the run is skipped and the reason logged. Nothing is retried.
- Jobs run on a schedule or on an event about the mesh (a node going quiet or coming back, a
  node's battery running low, the channel staying busy). Nothing triggers on incoming
  messages, so there are no auto-replies. An event fires once per change of state, never on
  what's already true when watching starts. Event jobs default to "notify me" (a desktop
  notification here; nothing is sent).

Templates use {placeholders}, optionally with a Python format spec: "{weather.temp:.0f}".
Write {{ and }} for literal braces. Sources:
  {time} {date} {time_utc} {date_utc}                        this computer's clock
  {clock.offset_ms} {clock.server}                           NIST NTP check of that clock
  {station.channel_util} {station.air_util_tx} {station.battery} {station.voltage}
  {station.nodes_heard_24h} {station.nodes_heard_7d} {station.packets_24h}
  {station.direct_neighbors_24h}                             from this station's log
  {node.<short name or !id>.battery|voltage|snr|hops|name|long_name|last_heard}
  {weather.name|short|detail|temp|temp_unit|wind|wind_dir}   National Weather Service forecast
  {<source>.<field>}                                         your HTTP/JSON sources
  {cmd.<name>}                                               a local command's first line (opt-in)
  {event.node|node_long|node_id|quiet_for|last_heard|battery|channel_util|threshold}   event jobs
"""

import hashlib
import json
import logging
import re
import shlex
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from .api import MAX_TEXT_BYTES, ApiError
from .coverage import station_from_store

log = logging.getLogger("meshshack")

MIN_EVERY_HOURS = 6
JITTER_SECONDS = 600  # each run starts up to 10 minutes after its slot, so bots don't all fire at :00
STALE_AFTER = 3600  # a slot missed by more than this (e.g. the logger was off) is skipped, not sent late
TICK_SECONDS = 30
USER_AGENT = "MeshShack/0.1 (https://github.com/JLF21-dev/meshshack)"
NIST_SERVER = "time.nist.gov"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Starting points offered in the app. Each starts in dry-run mode like any other job.
PRESETS = {
    "Weekly NIST time check": {
        "trigger": {"type": "weekly", "weekday": 6, "at": "12:00"},
        "template": "NIST time check {date_utc} {time_utc} UTC: this station's clock is {clock.offset_ms:+.0f} ms "
                    "off. Check yours!",
        "sources": [],
    },
    "Daily weather": {
        "trigger": {"type": "daily", "at": "07:00"},
        "template": "Weather {weather.name}: {weather.short}, {weather.temp}°{weather.temp_unit}, wind "
                    "{weather.wind} {weather.wind_dir}. (NWS)",
        "sources": [],
    },
    "Weekly mesh stats": {
        "trigger": {"type": "weekly", "weekday": 0, "at": "19:00"},
        "template": "Mesh stats: {station.nodes_heard_7d} nodes heard this week, "
                    "{station.direct_neighbors_24h} directly today; channel busy {station.channel_util:.0f}%.",
        "sources": [],
    },
}


PRESETS.update({
    "A favorite went quiet (notify me)": {
        "trigger": {"type": "node_quiet", "node": "favorites", "hours": 6},
        "destination": {"notify": True},
        "template": "{event.node} ({event.node_long}) hasn't been heard for {event.quiet_for}.",
        "sources": [],
    },
    "A favorite is back (notify me)": {
        "trigger": {"type": "node_back", "node": "favorites", "hours": 6},
        "destination": {"notify": True},
        "template": "{event.node} is back after about {event.quiet_for} quiet.",
        "sources": [],
    },
    "Low battery on a favorite (notify me)": {
        "trigger": {"type": "battery_low", "node": "favorites", "below": 20},
        "destination": {"notify": True},
        "template": "{event.node}'s battery is down to {event.battery}%.",
        "sources": [],
    },
    "Channel busy (notify me)": {
        "trigger": {"type": "channel_busy", "above": 25, "minutes": 15},
        "destination": {"notify": True},
        "template": "The channel has been over {event.threshold}% busy ({event.channel_util:.0f}% average) "
                    "for 15 minutes.",
        "sources": [],
    },
})

# What an event job's preview is filled in with, since there may be no real event at hand.
SAMPLE_EVENT = {"node": "CMP", "node_long": "Campus Router", "node_id": "!11111111", "quiet_for": "7 h",
                "last_heard": "7 h ago", "battery": 18, "channel_util": 27.5, "threshold": 25}


class RenderError(Exception):
    """A template couldn't be filled in; the run is skipped with this as the reason."""


def gate_source(job):
    """How the gatekeeper knows this job: by id, so renaming a job doesn't reset its limits."""
    return f"automation:job{job['id']}"


NAME = re.compile(r"[A-Za-z0-9][\w .'-]{0,39}")
SOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,19}")
RESERVED = {"time", "date", "time_utc", "date_utc", "clock", "station", "node", "weather", "cmd"}


def validate_job(job):
    """Check a job before it's saved; raises ValueError with a message for the user."""
    if not NAME.fullmatch(job.get("name") or ""):
        raise ValueError("name: 1-40 letters, digits, spaces, . ' - _")
    validate_trigger(job.get("trigger") or {})
    dest = job.get("destination") or {}
    if (job.get("trigger") or {}).get("type") == "channel_busy" and not dest.get("notify"):
        raise ValueError("a busy-channel job can only notify you: sending then would add to the congestion "
                         "(and the airtime gatekeeper would refuse it anyway)")
    if dest.get("notify"):
        pass
    elif "to" in dest:
        if not isinstance(dest["to"], int) or not 0 < dest["to"] < 0xFFFFFFFF:
            raise ValueError("destination node must be a node number")
    elif not isinstance(dest.get("channel"), int) or not 0 <= dest["channel"] <= 7:
        raise ValueError("destination must be a channel (0-7) or a node")
    template = job.get("template") or ""
    if not template.strip() or len(template) > 1000:
        raise ValueError("the template must be 1-1000 characters")
    names = set()
    for s in job.get("sources") or []:
        kind = s.get("type", "http")
        if not SOURCE_NAME.fullmatch(s.get("name") or "") or s["name"] in RESERVED or s["name"] in names:
            raise ValueError(f"source name {s.get('name')!r}: lowercase letters, digits, _; unique; not a built-in name")
        names.add(s["name"])
        if kind == "http":
            if urllib.parse.urlparse(s.get("url") or "").scheme not in ("http", "https"):
                raise ValueError(f"source {s['name']}: the URL must start with http:// or https://")
        elif kind == "command":
            if not (s.get("command") or "").strip():
                raise ValueError(f"source {s['name']}: give a command")
        else:
            raise ValueError(f"source {s['name']}: unknown type {kind!r}")
    return job


# ---- scheduling ----

SCHEDULES = ("daily", "weekly", "every")
EVENTS = ("node_quiet", "node_back", "battery_low", "channel_busy")
NOTIFY_COOLDOWN = 1800  # a notify-only job tells you about the same node at most every 30 min


def _validate_node(value):
    if value != "favorites" and (not isinstance(value, int) or not 0 < value < 0xFFFFFFFF):
        raise ValueError("choose a node, or any favorite")


def validate_trigger(trigger):
    kind = trigger.get("type")
    if kind in ("node_quiet", "node_back"):
        _validate_node(trigger.get("node"))
        if not isinstance(trigger.get("hours"), (int, float)) or not 1 <= trigger["hours"] <= 720:
            raise ValueError("quiet means not heard for 1 to 720 hours")
        return trigger
    if kind == "battery_low":
        _validate_node(trigger.get("node"))
        if not isinstance(trigger.get("below"), int) or not 5 <= trigger["below"] <= 90:
            raise ValueError("battery threshold: 5 to 90%")
        return trigger
    if kind == "channel_busy":
        if not isinstance(trigger.get("above"), int) or not 5 <= trigger["above"] <= 90:
            raise ValueError("channel threshold: 5 to 90%")
        if not isinstance(trigger.get("minutes"), int) or not 5 <= trigger["minutes"] <= 240:
            raise ValueError("busy for 5 to 240 minutes")
        return trigger
    if kind in ("daily", "weekly"):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(trigger.get("at", ""))):
            raise ValueError("time must be HH:MM")
        if kind == "weekly" and trigger.get("weekday") not in range(7):
            raise ValueError("weekday must be 0 (Monday) to 6 (Sunday)")
    elif kind == "every":
        hours = trigger.get("hours")
        if not isinstance(hours, (int, float)) or hours < MIN_EVERY_HOURS:
            raise ValueError(f"repeat at most every {MIN_EVERY_HOURS} hours")
    else:
        raise ValueError("a trigger is a schedule (daily, weekly, every N hours) or an event")
    return trigger


def _node_text(store, node):
    if node == "favorites":
        return "a favorite"
    row = store.node(node) if store is not None else None
    return (row["short_name"] if row is not None and row["short_name"] else None) or f"!{node:08x}"


def describe_trigger(trigger, store=None):
    kind = trigger.get("type")
    if kind == "node_quiet":
        return f"when {_node_text(store, trigger['node'])} isn't heard for {trigger['hours']:g} h"
    if kind == "node_back":
        return f"when {_node_text(store, trigger['node'])} is back after {trigger['hours']:g} h quiet"
    if kind == "battery_low":
        return f"when {_node_text(store, trigger['node'])}'s battery drops below {trigger['below']}%"
    if kind == "channel_busy":
        return f"when the channel is over {trigger['above']}% for {trigger['minutes']} min"
    if kind == "daily":
        return f"daily at {trigger['at']}"
    if kind == "weekly":
        return f"{WEEKDAYS[trigger['weekday']]}s at {trigger['at']}"
    if kind == "every":
        return f"every {trigger['hours']:g} hours"
    return "?"


def latest_slot(trigger, now, anchor=0.0):
    """The most recent scheduled time at or before `now` (local time), or None."""
    kind = trigger["type"]
    if kind == "every":
        period = trigger["hours"] * 3600
        return anchor + ((now - anchor) // period) * period if now >= anchor else None
    hour, minute = map(int, trigger["at"].split(":"))
    t = time.localtime(now)
    today = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
    if kind == "daily":
        return today if today <= now else today - 86400  # (DST days are 23/25 h; close enough)
    days_back = (t.tm_wday - trigger["weekday"]) % 7
    slot = today - days_back * 86400
    return slot if slot <= now else slot - 7 * 86400


def next_slot(trigger, now, anchor=0.0):
    kind = trigger["type"]
    if kind == "every":
        period = trigger["hours"] * 3600
        return (latest_slot(trigger, now, anchor) or anchor) + period
    step = 86400 if kind == "daily" else 7 * 86400
    return latest_slot(trigger, now, anchor) + step


def jitter(job_id, slot):
    """A steady per-run delay, so the same run always gets the same one."""
    digest = hashlib.sha256(f"{job_id}:{int(slot)}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % JITTER_SECONDS


# ---- data sources ----

def _dig(data, path):
    """Follow 'a.b.0.c' through dicts and lists."""
    for part in path.split("."):
        if isinstance(data, list) and part.lstrip("-").isdigit():
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(path)
    return data


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000))


def ntp_offset_ms(server=NIST_SERVER, timeout=5):
    """This computer's clock minus the server's, in ms: one SNTP exchange, using the four NTP
    timestamps so network delay cancels out. The name is resolved before timing starts."""
    address = socket.getaddrinfo(server, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        s.sendto(b"\x23" + 47 * b"\0", address)  # LI 0, version 4, mode 3 (client)
        data, _ = s.recvfrom(48)
        t4 = time.time()
    if len(data) < 48:
        raise OSError("short NTP reply")

    def stamp(offset):
        secs, frac = struct.unpack("!II", data[offset:offset + 8])
        return secs - 2208988800 + frac / 2**32

    t2, t3 = stamp(32), stamp(40)  # server receive and transmit times
    server_ahead = ((t2 - t1) + (t3 - t4)) / 2
    return -server_ahead * 1000


class Sources:
    """Resolves placeholders for one render. Remote results are cached across renders."""

    _cache = {}  # key -> (expires_at, value)
    _cache_lock = threading.Lock()

    def __init__(self, store, job=None, status=None, now=None, allow_commands=False, event=None):
        self.store = store
        self.event = event
        self.job = job or {}
        self.status = status or {}
        self.now = now or time.time()
        self.allow_commands = allow_commands
        sources = self.job.get("sources", [])
        self.http = {s["name"]: s for s in sources if s.get("type", "http") == "http" and s.get("name")}
        self.commands = {s["name"]: s for s in sources if s.get("type") == "command" and s.get("name")}

    @classmethod
    def _cached(cls, key, ttl, fn):
        with cls._cache_lock:
            hit = cls._cache.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
        value = fn()
        with cls._cache_lock:
            cls._cache[key] = (time.time() + ttl, value)
        return value

    def get(self, key):
        head, _, rest = key.partition(".")
        local, utc = time.localtime(self.now), time.gmtime(self.now)
        simple = {
            "time": time.strftime("%H:%M", local), "date": time.strftime("%a %b %-d", local),
            "time_utc": time.strftime("%H:%M", utc), "date_utc": time.strftime("%Y-%m-%d", utc),
        }
        if key in simple:
            return simple[key]
        if head == "event":
            if self.event is None or rest not in self.event or self.event[rest] is None:
                raise KeyError(key)
            return self.event[rest]
        if head == "clock":
            offset = self._cached("clock", 600, ntp_offset_ms)
            return {"offset_ms": offset, "server": NIST_SERVER}[rest]
        if head == "station":
            return self._station(rest)
        if head == "node":
            name, _, field = rest.rpartition(".")
            return self._node(name, field)
        if head == "weather":
            return self._weather()[rest]
        if head == "cmd":
            return self._command(rest)
        if head in self.http:
            return self._http(self.http[head], rest)
        raise KeyError(key)

    def _station(self, field):
        day, week = self.now - 86400, self.now - 7 * 86400
        q = self.store._query
        if field in ("channel_util", "air_util_tx", "battery", "voltage"):
            rows = q("""SELECT t.metrics FROM telemetry t JOIN packets p ON p.id = t.packet_row
                        WHERE p.is_local = 1 AND t.kind = 'deviceMetrics' ORDER BY t.id DESC LIMIT 1""")
            if not rows:
                raise KeyError("station telemetry")
            m = json.loads(rows[0]["metrics"])
            return {"channel_util": m.get("channelUtilization"), "air_util_tx": m.get("airUtilTx"),
                    "battery": m.get("batteryLevel"), "voltage": m.get("voltage")}[field]
        if field in ("nodes_heard_24h", "nodes_heard_7d"):
            since = day if field.endswith("24h") else week
            return q("SELECT COUNT(DISTINCT from_num) AS c FROM packets WHERE is_local = 0 AND logged_at >= ?",
                     (since,))[0]["c"]
        if field == "packets_24h":
            return q("SELECT COUNT(*) AS c FROM packets WHERE is_local = 0 AND logged_at >= ?", (day,))[0]["c"]
        if field == "direct_neighbors_24h":
            return len(self.store.coverage(day)["direct"])
        raise KeyError(field)

    def _node(self, name, field):
        rows = self.store._query("SELECT * FROM nodes WHERE short_name = ? OR node_id = ? ORDER BY last_heard DESC",
                                 (name, name))
        if not rows:
            raise KeyError(f"node {name}")
        n = rows[0]
        values = {"battery": n["battery_level"], "voltage": n["voltage"], "snr": n["last_snr"], "hops": n["hops_away"],
                  "name": n["short_name"], "long_name": n["long_name"]}
        if field == "last_heard":
            return f"{int((self.now - n['last_heard']) // 60)} min ago" if n["last_heard"] else "never"
        value = values[field]
        if value is None:
            raise KeyError(f"node {name} has no {field}")
        return value

    def _weather(self):
        lat, lon, _ = station_from_store(self.store)
        if lat is None:
            raise RenderError("weather needs this station's position (Device tab)")
        # Rounded to ~1 km before it leaves this computer; the forecast grid is 2.5 km anyway.
        point = f"{round(lat, 2)},{round(lon, 2)}"
        forecast_url = self._cached(f"nws-point:{point}", 86400,
                                    lambda: fetch_json(f"https://api.weather.gov/points/{point}")["properties"]["forecast"])
        period = self._cached(f"nws:{forecast_url}", 1800, lambda: fetch_json(forecast_url)["properties"]["periods"][0])
        return {"name": period["name"], "short": period["shortForecast"], "detail": period["detailedForecast"],
                "temp": period["temperature"], "temp_unit": period["temperatureUnit"],
                "wind": period["windSpeed"], "wind_dir": period["windDirection"]}

    def _http(self, source, field):
        url = source["url"]
        ttl = max(60, int(source.get("cache_minutes", 30)) * 60)
        data = self._cached(f"http:{url}", ttl, lambda: fetch_json(url, timeout=int(source.get("timeout", 10))))
        path = (source.get("fields") or {}).get(field, field)
        return _dig(data, path)

    def _command(self, name):
        if not self.allow_commands:
            raise RenderError("local commands are turned off (Automation tab)")
        source = self.commands.get(name)
        if source is None:
            raise KeyError(f"cmd.{name}")
        out = subprocess.run(shlex.split(source["command"]), capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""


def _ago(seconds):
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 1.5 else f"{max(1, round(seconds / 60))} min"


def _node_event(n, now, **extra):
    return {"node": n["short_name"] or n["node_id"], "node_long": n["long_name"] or n["short_name"] or n["node_id"],
            "node_id": n["node_id"], "battery": n["battery_level"],
            "last_heard": f"{_ago(now - n['last_heard'])} ago" if n["last_heard"] else "never", **extra}


def evaluate(store, job, now):
    """Update an event job's per-subject state and return the events that just happened, as
    [(subject, event values)]. The first look at a subject only records its state: an event is a
    change, never something that was already true when watching started."""
    t, fired = job["trigger"], []

    def step(subject, new, event, at=None):
        """at: when the new state really began (e.g. when a quiet node was last heard)."""
        prev = store.automation_state(job["id"], subject)
        if prev is None or prev["state"] != new:
            store.set_automation_state(job["id"], subject, new, at or now)
        if prev is not None and prev["state"] != new and event is not None:
            fired.append((subject, event(prev)))

    if t["type"] in ("node_quiet", "node_back", "battery_low"):
        if t["node"] == "favorites":
            me = store.my_num()
            subjects = [n for n in store.nodes() if n["is_favorite"] and n["num"] != me]
        else:
            subjects = [n for n in [store.node(t["node"])] if n is not None]
        for n in subjects:
            if t["type"] == "battery_low":
                b = n["battery_level"]
                if b is None or b > 100:  # unknown, or on external power
                    continue
                prev = store.automation_state(job["id"], n["num"])
                new = "low" if b < t["below"] else "ok" if b >= t["below"] + 10 else (prev["state"] if prev else "ok")
                step(n["num"], new, (lambda p, n=n: _node_event(n, now, threshold=t["below"]))
                     if new == "low" else None)
                continue
            quiet = n["last_heard"] is None or now - n["last_heard"] >= t["hours"] * 3600
            # A quiet state is dated from when the node was last heard, so "back after" is the real absence.
            since = n["last_heard"] if quiet else None
            if t["type"] == "node_quiet":
                step(n["num"], "quiet" if quiet else "ok",
                     (lambda p, n=n: _node_event(n, now, quiet_for=_ago(now - (n["last_heard"] or now)),
                                                 threshold=t["hours"])) if quiet else None, at=since)
            else:  # node_back: it was quiet (for at least `hours`) and has been heard again
                step(n["num"], "quiet" if quiet else "ok",
                     (lambda p, n=n: _node_event(n, now, threshold=t["hours"], quiet_for=_ago(now - p["changed_at"])))
                     if not quiet else None, at=since)
    elif t["type"] == "channel_busy":
        rows = store._query(
            """SELECT t.metrics FROM telemetry t JOIN packets p ON p.id = t.packet_row
               WHERE p.is_local = 1 AND t.kind = 'deviceMetrics' AND t.logged_at >= ?""", (now - t["minutes"] * 60,))
        utils = [json.loads(r["metrics"]).get("channelUtilization") for r in rows]
        utils = [u for u in utils if isinstance(u, (int, float))]
        if len(utils) >= 3:  # need a few readings to call it sustained
            avg = sum(utils) / len(utils)
            prev = store.automation_state(job["id"], 0)
            new = "busy" if avg > t["above"] else "ok" if avg < t["above"] - 5 else (prev["state"] if prev else "ok")
            step(0, new, (lambda p: {"channel_util": avg, "threshold": t["above"]}) if new == "busy" else None)
    return fired


PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][\w.!\-]*)(?::([^{}]*))?\}")


def render(template, sources):
    """Fill in a template. Raises RenderError if any placeholder can't be filled or it's too long."""
    def fill(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        key, spec = m.group(1), m.group(2)
        try:
            value = sources.get(key)
        except RenderError:
            raise
        except (KeyError, IndexError, TypeError):
            raise RenderError(f"no value for {{{key}}}")
        except (OSError, ValueError, subprocess.SubprocessError) as ex:
            raise RenderError(f"couldn't get {{{key}}}: {ex}")
        try:
            return format(value, spec) if spec else str(value)
        except (ValueError, TypeError):
            raise RenderError(f"can't format {{{key}}} as '{spec}'")

    text = PLACEHOLDER.sub(fill, template).strip()
    if not text:
        raise RenderError("the message is empty")
    size = len(text.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise RenderError(f"the message is {size} bytes; the limit is {MAX_TEXT_BYTES} (not sent, never truncated)")
    return text


# ---- the runner (in the logger) ----

class Automation:
    """Checks jobs every TICK_SECONDS and runs the ones that are due, through the gatekeeper."""

    def __init__(self, store, radio, clock=time.time):
        self.store = store
        self.radio = radio
        self.clock = clock
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="meshshack-automation", daemon=True).start()
        jobs = self.store.automation_jobs()
        log.info("Automation: %d job(s), %d live", len(jobs), sum(1 for j in jobs if j["enabled"] and not j["dry_run"]))

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("Automation tick failed")

    def tick(self):
        now = self.clock()
        for job in self.store.automation_jobs():
            if not job["enabled"]:
                continue
            if job["trigger"]["type"] in EVENTS:
                for subject, event in evaluate(self.store, job, now):
                    self.run(job, now, now, event=event, subject=subject)
                continue
            slot = latest_slot(job["trigger"], now, anchor=job["created_at"])
            if slot is None or slot <= job["created_at"] - 60:
                continue  # never fire for times before the job existed
            due = slot + jitter(job["id"], slot)
            if now < due or self.store.automation_ran(job["id"], slot):
                continue
            if now - due > STALE_AFTER:
                self.store.record_automation_run(job["id"], slot, "missed", None,
                                                 "the logger wasn't running at the scheduled time", now=now)
                continue
            self.run(job, slot, now)

    def run(self, job, slot, now, event=None, subject=None):
        status = self.radio.status() if self.radio is not None else {}
        sources = Sources(self.store, job, status, now, allow_commands=self.store.station("automation_commands") is True,
                          event=event)
        try:
            text = render(job["template"], sources)
        except RenderError as ex:
            return self.store.record_automation_run(job["id"], slot, "skipped", None, str(ex), now=now)
        dest = job["destination"]
        if dest.get("notify"):  # nothing is transmitted; the app shows it as a desktop notification
            recent = self.store.automation_runs(limit=20, job_id=job["id"])
            if any(r["status"] == "notified" and r["subject"] == subject and now - r["at"] < NOTIFY_COOLDOWN
                   for r in recent):
                return None  # already told you about this very recently
            return self.store.record_automation_run(job["id"], slot, "notified", text, None, now=now, subject=subject)
        if job["dry_run"]:
            return self.store.record_automation_run(job["id"], slot, "dry run", text, "dry run: nothing sent", now=now)
        try:
            result = self.radio.send_text(text, channel=dest.get("channel", 0), to=dest.get("to"),
                                          source=gate_source(job), allow_broadcast="to" not in dest)
        except ApiError as ex:  # refused by the gatekeeper, or the radio isn't connected
            return self.store.record_automation_run(job["id"], slot, "skipped", text, str(ex), now=now)
        log.info("Automation %r sent: %s", job["name"], text)
        return self.store.record_automation_run(job["id"], slot, "sent", text, result.get("warning"),
                                                packet_id=result.get("packet_id"), now=now)


def preview(store, job, status=None):
    """Render a job now with live data, without sending: (text or None, problem or None).
    An event job is filled in with SAMPLE_EVENT."""
    event = SAMPLE_EVENT if (job.get("trigger") or {}).get("type") in EVENTS else None
    try:
        text = render(job["template"], Sources(store, job, status, allow_commands=store.station("automation_commands") is True,
                                               event=event))
        return text, None
    except RenderError as ex:
        return None, str(ex)
