"""SQLite storage for everything the logger hears."""

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

BROADCAST_NUM = 0xFFFFFFFF

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    num            INTEGER PRIMARY KEY,
    node_id        TEXT NOT NULL,
    long_name      TEXT,
    short_name     TEXT,
    hw_model       TEXT,
    role           TEXT,
    first_seen     REAL NOT NULL,
    last_heard     REAL,      -- any path, including MQTT
    rf_heard       REAL,      -- last heard over the air (any number of hops)
    direct_heard   REAL,      -- last heard directly (0 hops, not MQTT): when last_snr/last_rssi were measured
    last_snr       REAL,      -- signal of the last *direct* packet; relayed packets carry the relay's signal
    last_rssi      INTEGER,
    hops_away      INTEGER,
    latitude       REAL,
    longitude      REAL,
    altitude       INTEGER,
    battery_level  INTEGER,
    voltage        REAL,
    is_favorite    INTEGER NOT NULL DEFAULT 0,  -- as the radio has it (favorites get CLIENT_BASE priority)
    is_ignored     INTEGER NOT NULL DEFAULT 0,  -- the radio drops this node's packets
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   REAL NOT NULL,
    packet_id   INTEGER,
    from_num    INTEGER,
    from_id     TEXT,
    to_num      INTEGER,
    to_id       TEXT,
    channel     INTEGER,
    portnum     TEXT,
    rx_time     INTEGER,
    rx_snr      REAL,
    rx_rssi     INTEGER,
    hop_limit   INTEGER,
    hop_start   INTEGER,
    via_mqtt    INTEGER,
    is_local    INTEGER NOT NULL DEFAULT 0,  -- from this station itself (the radio's own reports over USB)
    json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS packets_logged_at ON packets (logged_at);
CREATE INDEX IF NOT EXISTS packets_from ON packets (from_num, logged_at);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_row      INTEGER NOT NULL REFERENCES packets (id),
    logged_at       REAL NOT NULL,
    from_num        INTEGER,
    latitude        REAL,
    longitude       REAL,
    altitude        INTEGER,
    sats_in_view    INTEGER,
    precision_bits  INTEGER,
    position_time   INTEGER
);
CREATE INDEX IF NOT EXISTS positions_from ON positions (from_num, logged_at);

CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_row  INTEGER NOT NULL REFERENCES packets (id),
    logged_at   REAL NOT NULL,
    from_num    INTEGER,
    kind        TEXT NOT NULL,
    metrics     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_from ON telemetry (from_num, kind, logged_at);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   REAL NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT
);

-- Traceroutes and requests sent through the hub, and what came back. A request with no
-- reply after REQUEST_TIMEOUT seconds is shown as unanswered; a late reply still lands here.
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at        REAL NOT NULL,
    packet_id      INTEGER NOT NULL,
    kind           TEXT NOT NULL,     -- traceroute, position, telemetry, nodeinfo
    to_num         INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending, answered, failed
    response_row   INTEGER REFERENCES packets (id),
    completed_at   REAL
);
CREATE INDEX IF NOT EXISTS requests_packet_id ON requests (packet_id);

-- Every transmission decision the airtime gatekeeper made (see airtime.py), allowed or refused.
CREATE TABLE IF NOT EXISTS tx_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    source      TEXT NOT NULL,     -- manual, api:<name>, automation:<job>
    kind        TEXT NOT NULL,     -- dm, broadcast, traceroute, request, announce, config
    to_num      INTEGER,
    channel     INTEGER,
    cost        INTEGER NOT NULL,
    allowed     INTEGER NOT NULL,
    reason      TEXT,              -- why it was refused, or a warning it was sent with
    packet_id   INTEGER
);
CREATE INDEX IF NOT EXISTS tx_log_at ON tx_log (at);

-- Tokens for other apps using the local API (see api.py). Only a hash of each token is kept.
-- scopes: comma-separated, "read" and optionally "send". Revoked tokens stay for the record.
CREATE TABLE IF NOT EXISTS api_tokens (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    token_hash       TEXT NOT NULL UNIQUE,
    scopes           TEXT NOT NULL,
    allow_broadcast  INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    last_used_at     REAL,
    revoked_at       REAL
);

-- Possible emergencies the logger noticed (see alerts.py). Nothing is ever sent in response.
CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               REAL NOT NULL,
    packet_row       INTEGER REFERENCES packets (id),
    packet_id        INTEGER,
    from_num         INTEGER,
    to_num           INTEGER,
    channel          INTEGER,
    portnum          TEXT,
    reason           TEXT NOT NULL,
    text             TEXT,
    via_mqtt         INTEGER NOT NULL DEFAULT 0,
    loud             INTEGER NOT NULL DEFAULT 1,   -- the app sounds an alarm for it
    acknowledged_at  REAL
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (acknowledged_at, loud);

-- Automation jobs (see automation.py) and every run: sent, dry run, skipped (and why), missed.
CREATE TABLE IF NOT EXISTS automation_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    enabled      INTEGER NOT NULL DEFAULT 1,
    dry_run      INTEGER NOT NULL DEFAULT 1,     -- new jobs only record what they would send
    trigger      TEXT NOT NULL,                  -- JSON: {"type": "daily"|"weekly"|"every", ...}
    destination  TEXT NOT NULL,                  -- JSON: {"channel": n} or {"to": node num}
    template     TEXT NOT NULL,
    sources      TEXT NOT NULL DEFAULT '[]',     -- JSON: HTTP/command sources
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    job_name   TEXT,
    slot       REAL NOT NULL,          -- the scheduled time this run was for
    at         REAL NOT NULL,
    status     TEXT NOT NULL,          -- sent, dry run, skipped, missed
    text       TEXT,
    detail     TEXT,
    packet_id  INTEGER,
    subject    INTEGER                 -- for event jobs: the node it was about (0 for the channel)
);
CREATE INDEX IF NOT EXISTS automation_runs_job ON automation_runs (job_id, slot);
-- Event jobs' last known state per subject (node, or 0 for the channel), to fire only on a change.
CREATE TABLE IF NOT EXISTS automation_state (
    job_id      INTEGER NOT NULL,
    subject     INTEGER NOT NULL,
    state       TEXT NOT NULL,
    changed_at  REAL NOT NULL,
    PRIMARY KEY (job_id, subject)
);

-- Changes other apps asked for that need your approval in the app (see api.py). They run only
-- once approved, exactly as requested; pending ones expire after a day.
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           REAL NOT NULL,
    token_name   TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    body         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, denied, failed, expired
    decided_at   REAL,
    result       TEXT
);

-- Small facts about this station that the radio doesn't hand back, e.g. the exact fixed
-- position (the radio only reports it rounded to the channel's position precision).
CREATE TABLE IF NOT EXISTS station (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- MeshCore: a separate mesh, heard through a second radio. Its nodes are known by public key
-- (64 hex characters). Hops come from the packet's path: 0 means heard straight from the sender.
CREATE TABLE IF NOT EXISTS mc_nodes (
    public_key    TEXT PRIMARY KEY,
    name          TEXT,
    type          INTEGER,               -- 1 chat, 2 repeater, 3 room server, 4 sensor
    latitude      REAL,
    longitude     REAL,
    first_seen    REAL NOT NULL,
    last_heard    REAL,                  -- an advert from it was heard (or, from the radio's list, sent)
    heard_by_us   REAL,                  -- this station heard one of its adverts itself
    advert_at     INTEGER,               -- its latest advert's own timestamp (the sender's clock)
    direct_heard  REAL,                  -- heard with no hops
    last_snr      REAL,                  -- from adverts heard directly only
    last_rssi     REAL,
    min_hops      INTEGER,               -- fewest hops any of its adverts took to get here
    last_hops     INTEGER,
    adverts       INTEGER NOT NULL DEFAULT 0,
    is_contact    INTEGER NOT NULL DEFAULT 0  -- in the radio's contact list
);

-- Every packet the MeshCore radio heard, as it arrived (it can't read most of them).
CREATE TABLE IF NOT EXISTS mc_packets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at     REAL NOT NULL,
    snr           REAL,
    rssi          REAL,
    route         TEXT,                  -- FLOOD, DIRECT, TC_FLOOD, TC_DIRECT
    payload_type  TEXT,                  -- ADVERT, GRP_TXT (channel), TEXT_MSG (direct), ACK, ...
    hops          INTEGER,
    path          TEXT,                  -- hex hashes of the repeaters it came through
    public_key    TEXT,                  -- the sender, when the packet says (adverts)
    channel_hash  TEXT,
    raw           TEXT NOT NULL          -- hex
);
CREATE INDEX IF NOT EXISTS mc_packets_logged_at ON mc_packets (logged_at);

CREATE TABLE IF NOT EXISTS mc_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at      REAL NOT NULL,
    direction      TEXT NOT NULL DEFAULT 'in',
    channel        INTEGER,              -- channel slot; NULL for a direct message
    channel_name   TEXT,
    sender         TEXT,                 -- channel messages: the name at the start of the text
    pubkey_prefix  TEXT,                 -- direct messages: the first 6 bytes of the sender's key
    text           TEXT,
    sent_at        INTEGER,              -- the sender's clock
    hops           INTEGER,              -- NULL when it came by a set route
    snr            REAL
);
CREATE INDEX IF NOT EXISTS mc_messages_logged_at ON mc_messages (logged_at);
"""

# Kept separate so the version-1 migration can rebuild the table.
# direction is 'in' (received) or 'out' (sent from this station). For outgoing messages,
# status moves sending -> relayed (a neighbor rebroadcast it) -> delivered (the
# destination acknowledged a DM), or to failed with the reason in status_detail.
MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_row     INTEGER REFERENCES packets (id),
    logged_at      REAL NOT NULL,
    packet_id      INTEGER,
    from_num       INTEGER,
    from_id        TEXT,
    to_num         INTEGER,
    to_id          TEXT,
    channel        INTEGER,
    is_direct      INTEGER NOT NULL,
    direction      TEXT NOT NULL DEFAULT 'in',
    text           TEXT,
    reply_id       INTEGER,
    emoji          INTEGER,
    status         TEXT,
    status_detail  TEXT,
    status_at      REAL
);
CREATE INDEX IF NOT EXISTS messages_logged_at ON messages (logged_at);
CREATE INDEX IF NOT EXISTS messages_packet_id ON messages (packet_id);
"""

SCHEMA_VERSION = 8  # v7: MeshCore tables (created by SCHEMA); v8: alerts from MeshCore too
REQUEST_TIMEOUT = 180


def node_id(num):
    """Meshtastic's canonical node ID string, e.g. '!a1b2c3d4'."""
    return f"!{num:08x}"


def to_jsonable(obj):
    """Strip protobuf objects and bytes out of a packet dict so it can be JSON-encoded."""
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items() if k != "raw"}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


# A packet heard straight from its sender: over the air (not MQTT), zero hops taken. Its SNR/RSSI
# describe that node; any other packet's describe the last relay or the MQTT gateway.
DIRECT_SQL = ("COALESCE(p.via_mqtt, 0) = 0 AND p.rx_snr IS NOT NULL AND p.hop_start IS NOT NULL"
              " AND p.hop_start = p.hop_limit")


def path_kind(rf, mqtt):
    """How a node's traffic reaches us, from its counts in heard_via(): 'radio', 'mqtt', 'both',
    or 'unknown' when the logger has no packets from it (it's only in the radio's node list)."""
    if rf and mqtt:
        return "both"
    if mqtt:
        return "mqtt"
    return "radio" if rf else "unknown"


TOKEN_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def hop_limit(packet):
    """The packet's remaining hop limit. Protobuf-to-dict conversion drops zero fields, so a packet
    that used up all its hops has no hopLimit; with a hopStart present, that means 0. Without a
    hopStart (older firmware) it's genuinely unknown."""
    limit = packet.get("hopLimit")
    if limit is None and packet.get("hopStart") is not None:
        return 0
    return limit


def hops_taken(packet):
    start, limit = packet.get("hopStart"), hop_limit(packet)
    if start is None or limit is None:
        return None
    return start - limit


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()
        # Acks that arrived before the send was recorded (see record_outgoing_message).
        self._early_acks = {}

    def _migrate(self):
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(messages)")]
            if cols and "direction" not in cols:
                # v0 messages table: packet_row was NOT NULL and there was no direction/status.
                shared = ", ".join(c for c in cols if c != "id")
                self._conn.executescript(f"""
                    BEGIN;
                    DROP INDEX IF EXISTS messages_logged_at;
                    ALTER TABLE messages RENAME TO messages_v0;
                    {MESSAGES_SCHEMA}
                    INSERT INTO messages (id, {shared}) SELECT id, {shared} FROM messages_v0;
                    DROP TABLE messages_v0;
                    COMMIT;
                """)
        self._conn.executescript(MESSAGES_SCHEMA)
        if version < 3:  # v3 reruns the v2 recompute: v2 briefly kept the radio's snr for "0-hop" nodes
            self._migrate_v2()
        if version < 4:
            with self._conn:
                self._add_column("nodes", "is_favorite", "INTEGER NOT NULL DEFAULT 0")
                self._add_column("nodes", "is_ignored", "INTEGER NOT NULL DEFAULT 0")
        if version < 5:
            # A packet that used up all its hops was stored with no hop limit (see hop_limit());
            # fill in the 0 and recompute the hop counts that depend on it.
            with self._conn:
                self._conn.execute("UPDATE packets SET hop_limit = 0 WHERE hop_start IS NOT NULL AND hop_limit IS NULL")
            self._migrate_v2()
        if version < 6:
            with self._conn:
                self._add_column("automation_runs", "subject", "INTEGER")
        if version < 8:
            with self._conn:
                self._add_column("alerts", "network", "TEXT NOT NULL DEFAULT 'meshtastic'")
                self._add_column("alerts", "sender", "TEXT")  # MeshCore: the name the message gave
                self._add_column("alerts", "mc_message", "INTEGER")
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _add_column(self, table, column, decl):
        if column not in [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _migrate_v2(self):
        """Signal columns used to follow every packet, so relayed and MQTT packets left the relay's
        or gateway's SNR/RSSI (and meaningless hop counts) on distant nodes. Add the columns that
        keep them apart and recompute them from the packet log."""
        with self._conn:
            self._add_column("nodes", "rf_heard", "REAL")
            self._add_column("nodes", "direct_heard", "REAL")
            self._add_column("packets", "is_local", "INTEGER NOT NULL DEFAULT 0")
            self._conn.executescript("""
                UPDATE nodes SET
                  rf_heard = (SELECT MAX(logged_at) FROM packets p WHERE p.from_num = nodes.num
                              AND COALESCE(p.via_mqtt, 0) = 0 AND p.rx_snr IS NOT NULL),
                  direct_heard = (SELECT MAX(logged_at) FROM packets p WHERE p.from_num = nodes.num AND {direct});
                UPDATE nodes SET
                  last_snr = (SELECT rx_snr FROM packets p WHERE p.from_num = nodes.num AND {direct}
                              ORDER BY p.id DESC LIMIT 1),
                  last_rssi = (SELECT rx_rssi FROM packets p WHERE p.from_num = nodes.num AND {direct}
                               ORDER BY p.id DESC LIMIT 1);
                UPDATE nodes SET hops_away = (
                    SELECT p.hop_start - p.hop_limit FROM packets p WHERE p.from_num = nodes.num
                    AND COALESCE(p.via_mqtt, 0) = 0 AND p.rx_snr IS NOT NULL AND p.hop_start IS NOT NULL
                    ORDER BY p.id DESC LIMIT 1)
                  WHERE rf_heard IS NOT NULL;
            """.format(direct=DIRECT_SQL))

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- writes -------------------------------------------------------

    def set_station(self, key, value, now=None):
        """Store a JSON-able value about this station; None deletes it."""
        with self._lock, self._conn:
            if value is None:
                self._conn.execute("DELETE FROM station WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    """INSERT INTO station (key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                    (key, json.dumps(value), now or time.time()),
                )

    def station(self, key):
        rows = self._query("SELECT value FROM station WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def record_tx(self, at, source, kind, to_num, channel, cost, allowed, reason=None):
        with self._lock, self._conn:
            return self._conn.execute(
                """INSERT INTO tx_log (at, source, kind, to_num, channel, cost, allowed, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (at, source, kind, to_num, channel, cost, int(allowed), reason),
            ).lastrowid

    def set_tx_packet(self, row, packet_id):
        with self._lock, self._conn:
            self._conn.execute("UPDATE tx_log SET packet_id = ? WHERE id = ?", (packet_id, row))

    def tx_allowed_since(self, since):
        """Allowed sends since a time, newest first: what the gatekeeper's budgets count."""
        return self._query("SELECT * FROM tx_log WHERE allowed = 1 AND at >= ? ORDER BY at DESC, id DESC", (since,))

    def tx_log(self, limit=50):
        return self._query("SELECT * FROM tx_log ORDER BY id DESC LIMIT ?", (limit,))

    def record_event(self, kind, detail=None, now=None):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (logged_at, kind, detail) VALUES (?, ?, ?)",
                (now or time.time(), kind, detail),
            )

    def record_packet(self, packet, now=None, local=False):
        """Store one received packet and everything derivable from it. Returns the packets row id.
        local: the packet came from this station itself (its own reports over USB), not the air."""
        now = now or time.time()
        decoded = packet.get("decoded") or {}
        from_num = packet.get("from")
        to_num = packet.get("to")
        channel = packet.get("channel", 0)
        if decoded:
            portnum = decoded.get("portnum", "UNKNOWN_APP")
        else:
            portnum = "ENCRYPTED" if "encrypted" in packet else "UNKNOWN"

        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO packets (logged_at, packet_id, from_num, from_id, to_num, to_id,
                       channel, portnum, rx_time, rx_snr, rx_rssi, hop_limit, hop_start, via_mqtt, is_local, json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    packet.get("id"),
                    from_num,
                    packet.get("fromId") or (node_id(from_num) if from_num else None),
                    to_num,
                    packet.get("toId") or (node_id(to_num) if to_num is not None else None),
                    channel,
                    portnum,
                    packet.get("rxTime"),
                    packet.get("rxSnr"),
                    packet.get("rxRssi"),
                    hop_limit(packet),
                    packet.get("hopStart"),
                    int(bool(packet.get("viaMqtt"))),
                    int(bool(local)),
                    json.dumps(to_jsonable(packet)),
                ),
            )
            row = cur.lastrowid

            if not from_num:
                return row

            # Only over-the-air packets say anything about hops, and only direct ones about the
            # sender's signal (see DIRECT_SQL). _upsert_node ignores the None values.
            hops = hops_taken(packet)
            over_air = not local and not packet.get("viaMqtt") and packet.get("rxSnr") is not None
            direct = over_air and hops == 0
            self._upsert_node(
                from_num,
                now,
                last_heard=now,
                rf_heard=now if over_air else None,
                direct_heard=now if direct else None,
                last_snr=packet.get("rxSnr") if direct else None,
                last_rssi=packet.get("rxRssi") if direct else None,
                hops_away=hops if over_air else None,
            )

            self._resolve_request(decoded, portnum, row, now)

            if portnum in ("TEXT_MESSAGE_APP", "ALERT_APP"):
                text = decoded.get("text")
                if text is None and isinstance(decoded.get("payload"), (bytes, bytearray)):
                    text = bytes(decoded["payload"]).decode("utf-8", errors="replace")  # ALERT_APP isn't decoded
                self._conn.execute(
                    """INSERT INTO messages (packet_row, logged_at, packet_id, from_num, from_id,
                           to_num, to_id, channel, is_direct, text, reply_id, emoji)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row,
                        now,
                        packet.get("id"),
                        from_num,
                        packet.get("fromId") or node_id(from_num),
                        to_num,
                        packet.get("toId"),
                        channel,
                        int(to_num is not None and to_num != BROADCAST_NUM),
                        text,
                        decoded.get("replyId"),
                        decoded.get("emoji"),
                    ),
                )
            elif portnum == "POSITION_APP" and "latitude" in (decoded.get("position") or {}):
                pos = decoded["position"]
                self._conn.execute(
                    """INSERT INTO positions (packet_row, logged_at, from_num, latitude, longitude,
                           altitude, sats_in_view, precision_bits, position_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row,
                        now,
                        from_num,
                        pos.get("latitude"),
                        pos.get("longitude"),
                        pos.get("altitude"),
                        pos.get("satsInView"),
                        pos.get("precisionBits"),
                        pos.get("time"),
                    ),
                )
                self._upsert_node(
                    from_num,
                    now,
                    latitude=pos.get("latitude"),
                    longitude=pos.get("longitude"),
                    altitude=pos.get("altitude"),
                )
            elif portnum == "TELEMETRY_APP" and decoded.get("telemetry"):
                for kind, metrics in decoded["telemetry"].items():
                    if kind in ("time", "raw") or not isinstance(metrics, dict):
                        continue
                    self._conn.execute(
                        """INSERT INTO telemetry (packet_row, logged_at, from_num, kind, metrics)
                           VALUES (?, ?, ?, ?, ?)""",
                        (row, now, from_num, kind, json.dumps(to_jsonable(metrics))),
                    )
                    if kind == "deviceMetrics":
                        self._upsert_node(
                            from_num,
                            now,
                            battery_level=metrics.get("batteryLevel"),
                            voltage=metrics.get("voltage"),
                        )
            elif portnum == "NODEINFO_APP" and decoded.get("user"):
                self._upsert_user(from_num, now, decoded["user"])
            elif portnum == "ROUTING_APP" and decoded.get("requestId"):
                error = (decoded.get("routing") or {}).get("errorReason", "NONE")
                self._apply_ack(decoded["requestId"], from_num, error, now)

        return row

    def record_request(self, packet_id, kind, to_num, now=None):
        """A traceroute or request the hub just sent; the reply is matched in record_packet."""
        with self._lock, self._conn:
            return self._conn.execute(
                "INSERT INTO requests (sent_at, packet_id, kind, to_num) VALUES (?, ?, ?, ?)",
                (now or time.time(), packet_id, kind, to_num),
            ).lastrowid

    def _resolve_request(self, decoded, portnum, row, now):
        """Called inside record_packet's transaction. A reply carries our packet id as requestId.
        A plain ACK (errorReason NONE) only means a neighbor relayed the request, so it's skipped;
        a routing error (e.g. NO_RESPONSE) is a failure unless a real reply already arrived."""
        request_id = decoded.get("requestId")
        if not request_id:
            return
        if portnum == "ROUTING_APP":
            error = (decoded.get("routing") or {}).get("errorReason", "NONE")
            if error == "NONE":
                return
            self._conn.execute(
                """UPDATE requests SET status = 'failed', response_row = ?, completed_at = ?
                   WHERE packet_id = ? AND status = 'pending'""",
                (row, now, request_id),
            )
        else:
            self._conn.execute(
                """UPDATE requests SET status = 'answered', response_row = ?, completed_at = ?
                   WHERE packet_id = ? AND status != 'answered'""",
                (row, now, request_id),
            )

    def requests(self, limit=200):
        """Recent requests, newest first, with the reply packet's JSON when there is one."""
        return self._query(
            """SELECT r.*, p.json AS response_json FROM requests r
               LEFT JOIN packets p ON p.id = r.response_row ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        )

    def set_node_flags(self, num, favorite=None, ignored=None):
        """Mirror a favorite/ignore change made on the radio, without waiting for its next report."""
        fields = {k: int(v) for k, v in (("is_favorite", favorite), ("is_ignored", ignored)) if v is not None}
        with self._lock, self._conn:
            self._upsert_node(num, time.time(), **fields)

    def record_alert(self, alert):
        with self._lock, self._conn:
            return self._conn.execute(
                """INSERT INTO alerts (at, packet_row, packet_id, from_num, to_num, channel, portnum, reason, text,
                       via_mqtt, loud, network, sender, mc_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (alert["at"], alert.get("packet_row"), alert.get("packet_id"), alert.get("from_num"),
                 alert.get("to_num"), alert.get("channel"), alert.get("portnum"), alert["reason"], alert["text"],
                 int(alert.get("via_mqtt", False)), int(alert["loud"]), alert.get("network", "meshtastic"),
                 alert.get("sender"), alert.get("mc_message")),
            ).lastrowid

    def alerts(self, limit=200, open_only=False, loud_only=False):
        """Newest first, with the sender's names."""
        where = ["1"]
        if open_only:
            where.append("a.acknowledged_at IS NULL")
        if loud_only:
            where.append("a.loud = 1")
        return self._query(
            f"""SELECT a.*, COALESCE(n.short_name, a.sender) AS from_short, n.long_name AS from_long,
                       COALESCE(n.node_id, CASE WHEN a.network = 'meshcore' THEN 'MeshCore' END) AS from_id,
                       COALESCE(p.rx_snr, mm.snr) AS rx_snr, COALESCE(p.hop_start - p.hop_limit, mm.hops) AS hops,
                       mm.pubkey_prefix AS mc_prefix
                FROM alerts a LEFT JOIN nodes n ON n.num = a.from_num LEFT JOIN packets p ON p.id = a.packet_row
                LEFT JOIN mc_messages mm ON mm.id = a.mc_message
                WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT ?""",
            (limit,),
        )

    def acknowledge_alerts(self, ids=None, now=None):
        """Acknowledge the given alert ids, or every open alert. Returns how many changed."""
        with self._lock, self._conn:
            if ids is None:
                cur = self._conn.execute("UPDATE alerts SET acknowledged_at = ? WHERE acknowledged_at IS NULL",
                                         (now or time.time(),))
            else:
                cur = self._conn.executemany(
                    "UPDATE alerts SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
                    [(now or time.time(), i) for i in ids])
            return cur.rowcount

    # ---- automation ----

    @staticmethod
    def _job(row):
        job = dict(row)
        for key in ("trigger", "destination", "sources"):
            job[key] = json.loads(job[key])
        job["enabled"], job["dry_run"] = bool(job["enabled"]), bool(job["dry_run"])
        return job

    def automation_jobs(self):
        return [self._job(r) for r in self._query("SELECT * FROM automation_jobs ORDER BY id")]

    def automation_job(self, job_id):
        rows = self._query("SELECT * FROM automation_jobs WHERE id = ?", (job_id,))
        return self._job(rows[0]) if rows else None

    def save_automation_job(self, job, now=None):
        """Insert (no id) or update a job; returns its id. New jobs start in dry-run mode."""
        now = now or time.time()
        values = (job["name"], int(job.get("enabled", True)), int(job.get("dry_run", True)),
                  json.dumps(job["trigger"]), json.dumps(job["destination"]), job["template"],
                  json.dumps(job.get("sources") or []))
        with self._lock, self._conn:
            try:
                if job.get("id") is None:
                    return self._conn.execute(
                        """INSERT INTO automation_jobs (name, enabled, dry_run, trigger, destination, template, sources,
                               created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (*values, now, now)).lastrowid
                old = self._conn.execute("SELECT trigger FROM automation_jobs WHERE id = ?", (job["id"],)).fetchone()
                self._conn.execute(
                    """UPDATE automation_jobs SET name = ?, enabled = ?, dry_run = ?, trigger = ?, destination = ?,
                           template = ?, sources = ?, updated_at = ? WHERE id = ?""", (*values, now, job["id"]))
                if old is not None and json.loads(old["trigger"]) != job["trigger"]:
                    # A changed trigger watches something else: start over (learn the state, don't fire).
                    self._conn.execute("DELETE FROM automation_state WHERE job_id = ?", (job["id"],))
                return job["id"]
            except sqlite3.IntegrityError:
                raise ValueError(f"there's already a job named {job['name']!r}")

    def delete_automation_job(self, job_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM automation_state WHERE job_id = ?", (job_id,))
            return self._conn.execute("DELETE FROM automation_jobs WHERE id = ?", (job_id,)).rowcount > 0

    def automation_state(self, job_id, subject):
        rows = self._query("SELECT * FROM automation_state WHERE job_id = ? AND subject = ?", (job_id, subject))
        return rows[0] if rows else None

    def set_automation_state(self, job_id, subject, state, now=None):
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO automation_state (job_id, subject, state, changed_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (job_id, subject) DO UPDATE SET state = excluded.state, changed_at = excluded.changed_at""",
                (job_id, subject, state, now or time.time()),
            )

    def my_num(self):
        """This station's node number, as the log knows it: the sender of its own (local) reports."""
        rows = self._query("SELECT from_num FROM packets WHERE is_local = 1 GROUP BY from_num ORDER BY COUNT(*) DESC LIMIT 1")
        return rows[0]["from_num"] if rows else None

    def automation_ran(self, job_id, slot):
        return bool(self._query("SELECT 1 FROM automation_runs WHERE job_id = ? AND slot = ?", (job_id, slot)))

    def record_automation_run(self, job_id, slot, status, text, detail, packet_id=None, now=None, subject=None):
        job = self.automation_job(job_id)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO automation_runs (job_id, job_name, slot, at, status, text, detail, packet_id, subject)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, job["name"] if job else None, slot, now or time.time(), status, text, detail, packet_id, subject),
            )
        return {"status": status, "text": text, "detail": detail}

    def automation_runs(self, limit=100, job_id=None):
        if job_id is None:
            return self._query("SELECT * FROM automation_runs ORDER BY id DESC LIMIT ?", (limit,))
        return self._query("SELECT * FROM automation_runs WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit))

    def mark_local(self, my_num):
        """Tag this station's own packets, including ones logged before local tagging existed."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE packets SET is_local = 1 WHERE from_num = ? AND is_local = 0", (my_num,))

    def record_outgoing_message(self, packet_id, from_num, to_num, channel, text, reply_id=None, now=None, emoji=False):
        """Record a text message this station sent (the radio doesn't echo these back).
        emoji: it's a reaction (tapback) to the message whose packet id is reply_id."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO messages (logged_at, packet_id, from_num, from_id, to_num, to_id,
                       channel, is_direct, direction, text, reply_id, emoji, status, status_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'out', ?, ?, ?, 'sending', ?)""",
                (
                    now,
                    packet_id,
                    from_num,
                    node_id(from_num) if from_num is not None else None,
                    to_num,
                    "^all" if to_num == BROADCAST_NUM else node_id(to_num),
                    channel,
                    int(to_num != BROADCAST_NUM),
                    text,
                    reply_id,
                    1 if emoji else None,
                    now,
                ),
            )
            # A fast NAK can be processed on the radio library's thread before we get here.
            for args in self._early_acks.pop(packet_id, []):
                self._apply_ack(packet_id, *args)
            return cur.lastrowid

    def _apply_ack(self, request_id, from_num, error, now):
        rows = self._conn.execute(
            "SELECT id, to_num, is_direct, status FROM messages WHERE direction = 'out' AND packet_id = ?",
            (request_id,),
        ).fetchall()
        if not rows:
            if len(self._early_acks) > 1000:  # acks for things other than our texts; don't grow forever
                self._early_acks.clear()
            self._early_acks.setdefault(request_id, []).append((from_num, error, now))
            return
        for msg in rows:
            if error == "NONE":
                new = "delivered" if msg["is_direct"] and from_num == msg["to_num"] else "relayed"
                if msg["status"] == "delivered" or (new == "relayed" and msg["status"] == "relayed"):
                    continue
                detail = None
            else:
                if msg["status"] != "sending":  # a late NAK doesn't undo a confirmed relay/delivery
                    continue
                new, detail = "failed", error
            self._conn.execute(
                "UPDATE messages SET status = ?, status_detail = ?, status_at = ? WHERE id = ?",
                (new, detail, now, msg["id"]),
            )

    def record_node_info(self, node, now=None):
        """Store a node entry from the radio's node database (interface.nodes)."""
        num = node.get("num")
        if num is None:
            return
        now = now or time.time()
        pos = node.get("position") or {}
        metrics = node.get("deviceMetrics") or {}
        # Signal and hops come from our own packets instead: the radio's snr follows every packet
        # (relayed or not), and a hop count of 0 is simply left out of its report, so a direct
        # neighbor can't be told apart from a node whose hops are unknown. A present hop count
        # is fine to use unless the node was last heard via MQTT.
        mqtt = bool(node.get("viaMqtt"))
        with self._lock, self._conn:
            self._upsert_node(
                num,
                now,
                last_heard=node.get("lastHeard"),
                hops_away=None if mqtt else node.get("hopsAway"),
                # The radio's report leaves these out when false, so absent means no.
                is_favorite=int(bool(node.get("isFavorite"))),
                is_ignored=int(bool(node.get("isIgnored"))),
                latitude=pos.get("latitude"),
                longitude=pos.get("longitude"),
                altitude=pos.get("altitude"),
                battery_level=metrics.get("batteryLevel"),
                voltage=metrics.get("voltage"),
            )
            if node.get("user"):
                self._upsert_user(num, now, node["user"])

    def _upsert_user(self, num, now, user):
        self._upsert_node(
            num,
            now,
            long_name=user.get("longName"),
            short_name=user.get("shortName"),
            hw_model=user.get("hwModel"),
            role=user.get("role", "CLIENT"),
        )

    def _upsert_node(self, num, now, **fields):
        # Only overwrite columns we actually have a value for. Column names come from
        # this module, never from packet contents.
        fields = {k: v for k, v in fields.items() if v is not None}
        cols = ["num", "node_id", "first_seen", "updated_at", *fields]
        values = [num, node_id(num), now, now, *fields.values()]
        updates = ["updated_at = excluded.updated_at"]
        for col in fields:
            if col == "last_heard":
                updates.append("last_heard = MAX(COALESCE(nodes.last_heard, 0), excluded.last_heard)")
            else:
                updates.append(f"{col} = excluded.{col}")
        self._conn.execute(
            f"""INSERT INTO nodes ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})
                ON CONFLICT (num) DO UPDATE SET {", ".join(updates)}""",
            values,
        )

    # ---- reads --------------------------------------------------------

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def node_label(self, num):
        if num is None:
            return "?"
        if num == BROADCAST_NUM:
            return "^all"
        rows = self._query("SELECT short_name FROM nodes WHERE num = ?", (num,))
        short = rows[0]["short_name"] if rows else None
        return f"{node_id(num)} ({short})" if short else node_id(num)

    def nodes(self, since=None):
        # Nodes from the radio's database may never have been heard (last_heard NULL);
        # show them unless a time window was asked for.
        if since is None:
            return self._query("SELECT * FROM nodes ORDER BY last_heard DESC NULLS LAST")
        return self._query(
            "SELECT * FROM nodes WHERE last_heard >= ? ORDER BY last_heard DESC", (since,)
        )

    def messages(self, limit=50, since=None):
        rows = self._query(
            """SELECT m.*, n.short_name AS from_short, n.long_name AS from_long,
                      p.rx_snr, p.rx_rssi
               FROM messages m
               LEFT JOIN nodes n ON n.num = m.from_num
               LEFT JOIN packets p ON p.id = m.packet_row
               WHERE m.logged_at >= ?
               ORDER BY m.logged_at DESC LIMIT ?""",
            (since or 0, limit),
        )
        return list(reversed(rows))

    def packets(self, limit=50, portnum=None, since=None, from_num=None):
        sql = """SELECT p.*, n.short_name AS from_short FROM packets p
                 LEFT JOIN nodes n ON n.num = p.from_num WHERE p.logged_at >= ?"""
        params = [since or 0]
        if portnum:
            sql += " AND p.portnum = ?"
            params.append(portnum)
        if from_num is not None:
            sql += " AND p.from_num = ?"
            params.append(from_num)
        sql += " ORDER BY p.logged_at DESC LIMIT ?"
        params.append(limit)
        return list(reversed(self._query(sql, params)))

    def data_version(self):
        """Changes whenever another connection (the logger) commits; cheap to poll."""
        return self._query("PRAGMA data_version")[0][0]

    def conversations(self):
        """Channels and direct-message peers that have messages, newest activity first."""
        return self._query(
            """SELECT CASE WHEN is_direct THEN 'dm' ELSE 'channel' END AS kind,
                      CASE WHEN NOT is_direct THEN channel
                           WHEN direction = 'out' THEN to_num ELSE from_num END AS key,
                      MAX(id) AS last_id, MAX(logged_at) AS last_at
               FROM messages GROUP BY kind, key ORDER BY last_at DESC"""
        )

    def thread(self, channel=None, peer=None, limit=500):
        """Messages in one conversation, oldest first: a channel index, or a DM peer's node number."""
        if peer is not None:
            where, params = "m.is_direct = 1 AND (m.from_num = ? OR m.to_num = ?)", [peer, peer]
        else:
            where, params = "m.is_direct = 0 AND m.channel = ?", [channel or 0]
        rows = self._query(
            f"""SELECT m.*, n.short_name AS from_short, n.long_name AS from_long,
                       p.rx_snr, p.rx_rssi, p.hop_start - p.hop_limit AS hops, p.via_mqtt, p.portnum,
                       (SELECT reason FROM alerts a WHERE a.packet_row = m.packet_row LIMIT 1) AS alert_reason
                FROM messages m
                LEFT JOIN nodes n ON n.num = m.from_num
                LEFT JOIN packets p ON p.id = m.packet_row
                WHERE {where} ORDER BY m.id DESC LIMIT ?""",
            [*params, limit],
        )
        return list(reversed(rows))

    def messages_by_packet(self, packet_ids):
        """{packet id: message row} for messages with these packet ids, in any conversation."""
        ids = [int(i) for i in packet_ids if i is not None]
        if not ids:
            return {}
        rows = self._query(
            f"""SELECT m.*, n.short_name AS from_short FROM messages m LEFT JOIN nodes n ON n.num = m.from_num
                WHERE m.packet_id IN ({','.join('?' * len(ids))}) AND COALESCE(m.emoji, 0) = 0""", ids)
        return {r["packet_id"]: r for r in rows}

    def unread_count(self, channel=None, peer=None, after_id=0):
        """Received messages in a conversation newer than after_id."""
        if peer is not None:
            where, params = "is_direct = 1 AND from_num = ?", [peer]
        else:
            where, params = "is_direct = 0 AND channel = ?", [channel or 0]
        return self._query(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND direction = 'in' AND id > ?",
            [*params, after_id],
        )[0][0]

    def node(self, num):
        rows = self._query("SELECT * FROM nodes WHERE num = ?", (num,))
        return rows[0] if rows else None

    def position_tracks(self, since):
        return self._query(
            """SELECT from_num, latitude, longitude FROM positions
               WHERE logged_at >= ? AND latitude IS NOT NULL ORDER BY from_num, logged_at""",
            (since,),
        )

    def position_precision(self):
        """{node num: precision_bits of its latest logged position} (None if the packet didn't say)."""
        rows = self._query(
            """SELECT p.from_num, p.precision_bits FROM positions p
               JOIN (SELECT from_num, MAX(id) AS id FROM positions GROUP BY from_num) latest ON latest.id = p.id"""
        )
        return {r["from_num"]: r["precision_bits"] for r in rows}

    def heard_via(self):
        """{node num: (packets heard over radio, packets that came through MQTT)} for every sender logged."""
        rows = self._query(
            """SELECT from_num, SUM(COALESCE(via_mqtt, 0) = 0) AS rf, SUM(COALESCE(via_mqtt, 0) != 0) AS mqtt
               FROM packets WHERE from_num IS NOT NULL AND is_local = 0 GROUP BY from_num"""
        )
        return {r["from_num"]: (r["rf"], r["mqtt"]) for r in rows}

    def telemetry(self, limit=50, kind=None, since=None, from_num=None):
        sql = """SELECT t.*, n.node_id AS from_id, n.short_name AS from_short FROM telemetry t
                 LEFT JOIN nodes n ON n.num = t.from_num WHERE t.logged_at >= ?"""
        params = [since or 0]
        if kind:
            sql += " AND t.kind = ?"
            params.append(kind)
        if from_num is not None:
            sql += " AND t.from_num = ?"
            params.append(from_num)
        sql += " ORDER BY t.logged_at DESC LIMIT ?"
        params.append(limit)
        return list(reversed(self._query(sql, params)))

    # Metrics charted per node: (key, deviceMetrics field). SNR comes from direct packets instead.
    CHART_METRICS = ("batteryLevel", "voltage", "channelUtilization", "airUtilTx")

    def node_series(self, num, since=0):
        """{metric: [(time, value), ...]} for charts: device telemetry fields, plus "directSnr"
        from packets heard straight from the node (the only signal figure that describes it)."""
        series = {key: [] for key in (*self.CHART_METRICS, "directSnr")}
        rows = self._query(
            """SELECT logged_at, metrics FROM telemetry
               WHERE from_num = ? AND kind = 'deviceMetrics' AND logged_at >= ? ORDER BY logged_at""",
            (num, since),
        )
        for r in rows:
            metrics = json.loads(r["metrics"])
            for key in self.CHART_METRICS:
                value = metrics.get(key)
                if isinstance(value, (int, float)):
                    series[key].append((r["logged_at"], float(value)))
        snr = self._query(
            f"SELECT logged_at, rx_snr FROM packets p WHERE from_num = ? AND logged_at >= ? AND {DIRECT_SQL} "
            "ORDER BY logged_at",
            (num, since),
        )
        series["directSnr"] = [(r["logged_at"], r["rx_snr"]) for r in snr]
        return series

    def coverage(self, since=0):
        """How this station hears the mesh, for the Coverage tab. Returns
          direct: {num: {"snrs": [...], "rssis": [...], "last": t}} for packets heard straight from
                  the node (see DIRECT_SQL);
          relays: {last byte of the relaying node's id: packet count} for over-the-air packets that
                  took at least one hop (the firmware only records the relayer's last byte);
          totals: {"heard", "direct", "relayed", "mqtt", "unknown_path"} packet counts (not local)."""
        direct = {}
        for r in self._query(
            f"SELECT from_num, rx_snr, rx_rssi, logged_at FROM packets p "
            f"WHERE logged_at >= ? AND is_local = 0 AND {DIRECT_SQL} ORDER BY logged_at",
            (since,),
        ):
            d = direct.setdefault(r["from_num"], {"snrs": [], "rssis": [], "last": 0})
            d["snrs"].append(r["rx_snr"])
            if r["rx_rssi"] is not None:
                d["rssis"].append(r["rx_rssi"])
            d["last"] = r["logged_at"]
        relays = {
            r["relay"]: r["c"]
            for r in self._query(
                """SELECT json_extract(json, '$.relayNode') AS relay, COUNT(*) AS c FROM packets
                   WHERE logged_at >= ? AND is_local = 0 AND COALESCE(via_mqtt, 0) = 0
                     AND hop_start IS NOT NULL AND hop_start > hop_limit
                     AND json_extract(json, '$.relayNode') IS NOT NULL
                   GROUP BY relay""",
                (since,),
            )
        }
        t = self._query(
            """SELECT COUNT(*) AS heard,
                      SUM(COALESCE(via_mqtt, 0) != 0) AS mqtt,
                      SUM(COALESCE(via_mqtt, 0) = 0 AND hop_start IS NOT NULL AND hop_start > hop_limit) AS relayed,
                      SUM(COALESCE(via_mqtt, 0) = 0 AND hop_start IS NULL) AS unknown_path
               FROM packets WHERE logged_at >= ? AND is_local = 0""",
            (since,),
        )[0]
        totals = {k: t[k] or 0 for k in ("heard", "mqtt", "relayed", "unknown_path")}
        totals["direct"] = sum(len(d["snrs"]) for d in direct.values())
        return {"direct": direct, "relays": relays, "totals": totals}

    def relayed_by(self, since=0):
        """Relayed over-the-air packets by (relay byte, sender, hops taken), for telling a relay's
        radio traffic apart from internet traffic it re-transmits (see paths.py)."""
        return self._query(
            """SELECT json_extract(json, '$.relayNode') AS relay, from_num, hop_start - hop_limit AS hops,
                      COUNT(*) AS c FROM packets
               WHERE logged_at >= ? AND is_local = 0 AND COALESCE(via_mqtt, 0) = 0
                 AND hop_start IS NOT NULL AND hop_start > hop_limit AND json_extract(json, '$.relayNode') IS NOT NULL
               GROUP BY relay, from_num, hops""",
            (since,),
        )

    def coverage_over_time(self, since, bucket):
        """Per time bucket (seconds wide, starting at `since`): packets heard, packets heard
        directly, distinct direct neighbors, and packets per relay byte. For "did moving the
        antenna help?". Returns [{"start", "heard", "direct", "neighbors", "relays": {byte: n}}]."""
        rows = self._query(
            f"""SELECT CAST((logged_at - ?) / ? AS INTEGER) AS b, COUNT(*) AS heard,
                       SUM(CASE WHEN {DIRECT_SQL} THEN 1 ELSE 0 END) AS direct,
                       COUNT(DISTINCT CASE WHEN {DIRECT_SQL} THEN from_num END) AS neighbors
                FROM packets p WHERE logged_at >= ? AND is_local = 0 GROUP BY b ORDER BY b""",
            (since, bucket, since),
        )
        relays = {}
        for r in self._query(
            """SELECT CAST((logged_at - ?) / ? AS INTEGER) AS b, json_extract(json, '$.relayNode') AS relay,
                      COUNT(*) AS c FROM packets
               WHERE logged_at >= ? AND is_local = 0 AND COALESCE(via_mqtt, 0) = 0
                 AND hop_start IS NOT NULL AND hop_start > hop_limit AND json_extract(json, '$.relayNode') IS NOT NULL
               GROUP BY b, relay""",
            (since, bucket, since),
        ):
            relays.setdefault(r["b"], {})[int(r["relay"])] = r["c"]
        return [{"start": since + r["b"] * bucket, "heard": r["heard"], "direct": r["direct"],
                 "neighbors": r["neighbors"], "relays": relays.get(r["b"], {})} for r in rows]

    def node_summary(self, num):
        """Packet counts by type, message count, and the latest traceroute for the detail panel."""
        by_type = self._query(
            """SELECT portnum, COUNT(*) AS c, SUM(COALESCE(via_mqtt, 0)) AS mqtt FROM packets
               WHERE from_num = ? AND is_local = 0 GROUP BY portnum ORDER BY c DESC""",
            (num,),
        )
        messages = self._query("SELECT COUNT(*) AS c FROM messages WHERE from_num = ? OR to_num = ?", (num, num))[0]["c"]
        trace = self._query(
            """SELECT r.*, p.json AS response_json FROM requests r LEFT JOIN packets p ON p.id = r.response_row
               WHERE r.to_num = ? AND r.kind = 'traceroute' ORDER BY r.id DESC LIMIT 1""",
            (num,),
        )
        return {"by_type": by_type, "messages": messages, "last_traceroute": trace[0] if trace else None}

    def positions(self, limit=500, since=None, from_num=None):
        sql = "SELECT * FROM positions WHERE logged_at >= ?"
        params = [since or 0]
        if from_num is not None:
            sql += " AND from_num = ?"
            params.append(from_num)
        sql += " ORDER BY logged_at DESC LIMIT ?"
        params.append(limit)
        return list(reversed(self._query(sql, params)))

    # ---- API tokens ----

    def create_token(self, name, scopes, allow_broadcast=False, now=None):
        """Returns the new token. It's shown once; only its hash is stored."""
        if not TOKEN_NAME.fullmatch(name or ""):
            raise ValueError("name: 1-32 of a-z, 0-9, - and _, starting with a letter or digit")
        if not set(scopes) <= {"read", "send", "config"} or "read" not in scopes:
            raise ValueError("scopes: read, plus optionally send and config")
        token = "mst_" + secrets.token_urlsafe(32)
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    """INSERT INTO api_tokens (name, token_hash, scopes, allow_broadcast, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, _token_hash(token), ",".join(sorted(scopes)), int(allow_broadcast), now or time.time()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"a token named {name!r} already exists (revoked tokens keep their name)")
        return token

    def token_for(self, token):
        """The live (not revoked) token row for a presented token, or None. Marks it used."""
        rows = self._query("SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                           (_token_hash(token),))
        if not rows:
            return None
        with self._lock, self._conn:
            self._conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (time.time(), rows[0]["id"]))
        return rows[0]

    def tokens(self):
        return self._query("SELECT * FROM api_tokens ORDER BY id")

    APPROVAL_TTL = 86400

    def request_approval(self, token_name, method, path, body, summary, now=None):
        with self._lock, self._conn:
            return self._conn.execute(
                "INSERT INTO approvals (at, token_name, method, path, body, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (now or time.time(), token_name, method, path, json.dumps(body), summary),
            ).lastrowid

    def approvals(self, limit=100, token_name=None, pending_only=False, now=None):
        """Newest first. Pending requests older than a day are marked expired first."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE approvals SET status = 'expired', decided_at = ? WHERE status = 'pending' AND at < ?",
                               (now or time.time(), (now or time.time()) - self.APPROVAL_TTL))
        where, params = ["1"], []
        if token_name is not None:
            where.append("token_name = ?")
            params.append(token_name)
        if pending_only:
            where.append("status = 'pending'")
        return self._query(f"SELECT * FROM approvals WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                           (*params, limit))

    def decide_approval(self, approval_id, status, result=None, now=None):
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, result = ? WHERE id = ? AND status = 'pending'",
                (status, now or time.time(), json.dumps(result) if result is not None else None, approval_id),
            ).rowcount > 0

    def revoke_token(self, name, now=None):
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                                     (now or time.time(), name))
        return cur.rowcount > 0

    def events(self, limit=20):
        return list(reversed(self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))))

    def stats(self, since=None):
        since = since or 0
        count = lambda sql: self._query(sql, (since,))[0]["c"]
        counts = {"packets": count("SELECT COUNT(*) AS c FROM packets WHERE logged_at >= ? AND is_local = 0")}
        counts.update({
            table: count(f"SELECT COUNT(*) AS c FROM {table} WHERE logged_at >= ?")
            for table in ("messages", "positions", "telemetry")
        })
        # This station's own status reports over USB: not traffic, so counted apart.
        counts["local reports"] = count("SELECT COUNT(*) AS c FROM packets WHERE logged_at >= ? AND is_local = 1")
        counts["nodes heard"] = self._query(
            "SELECT COUNT(*) AS c FROM nodes WHERE last_heard >= ?", (since,)
        )[0]["c"]
        by_port = self._query(
            """SELECT portnum, COUNT(*) AS c FROM packets WHERE logged_at >= ? AND is_local = 0
               GROUP BY portnum ORDER BY c DESC""",
            (since,),
        )
        return counts, by_port

    # ---- MeshCore ----

    MC_FLOOD_ROUTES = ("FLOOD", "TC_FLOOD")

    @staticmethod
    def mc_hops(route, path_len):
        """Hops a MeshCore packet took to reach us. A flooded packet's path lists every repeater
        it passed; a directly routed one carries the route still ahead, so only an empty one
        (sent straight to us) says anything."""
        if path_len is None:
            return None
        if route in Store.MC_FLOOD_ROUTES:
            return path_len
        return 0 if route in ("DIRECT", "TC_DIRECT") and path_len == 0 else None

    def mc_record_packet(self, d, now=None):
        """One packet from the radio's receive log (the meshcore library's RX_LOG_DATA payload).
        Adverts also update the sender's node. Returns the row id."""
        now = now or time.time()
        route, kind = d.get("route_typename"), d.get("payload_typename")
        hops = self.mc_hops(route, d.get("path_len"))
        with self._lock, self._conn:
            row = self._conn.execute(
                """INSERT INTO mc_packets (logged_at, snr, rssi, route, payload_type, hops, path, public_key,
                                           channel_hash, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, d.get("snr"), d.get("rssi"), route, kind, hops, d.get("path") or None, d.get("adv_key"),
                 d.get("chan_hash"), d.get("payload") or d.get("raw_hex") or ""),
            ).lastrowid
        if kind == "ADVERT" and d.get("adv_key"):
            self.mc_record_advert(d["adv_key"], name=d.get("adv_name"), type_=d.get("adv_type"),
                                  lat=d.get("adv_lat"), lon=d.get("adv_lon"), advert_at=d.get("adv_timestamp"),
                                  hops=hops, snr=d.get("snr"), rssi=d.get("rssi"), now=now)
        return row

    def mc_record_advert(self, key, name=None, type_=None, lat=None, lon=None, advert_at=None, hops=None,
                         snr=None, rssi=None, now=None):
        """An advert this station heard. The same advert usually arrives several times by different
        repeaters; each copy can lower the hop count, but it's counted once."""
        now = now or time.time()
        with self._lock, self._conn:
            old = self._conn.execute("SELECT * FROM mc_nodes WHERE public_key = ?", (key,)).fetchone()
            old = dict(old) if old else {"first_seen": now, "adverts": 0, "is_contact": 0}
            newer = advert_at is not None and (old.get("advert_at") is None or advert_at > old["advert_at"])
            has_position = lat is not None and lon is not None and (lat, lon) != (0, 0)
            direct = hops == 0
            row = {
                "public_key": key,
                "name": name or old.get("name"),
                "type": type_ if type_ is not None else old.get("type"),
                "latitude": lat if has_position and (newer or old.get("latitude") is None) else old.get("latitude"),
                "longitude": lon if has_position and (newer or old.get("longitude") is None) else old.get("longitude"),
                "first_seen": old["first_seen"],
                "last_heard": now,
                "heard_by_us": now,
                "advert_at": advert_at if newer else old.get("advert_at"),
                "direct_heard": now if direct else old.get("direct_heard"),
                "last_snr": snr if direct else old.get("last_snr"),
                "last_rssi": rssi if direct else old.get("last_rssi"),
                "min_hops": hops if hops is not None and (old.get("min_hops") is None or hops < old["min_hops"])
                else old.get("min_hops"),
                "last_hops": hops if hops is not None else old.get("last_hops"),
                "adverts": old["adverts"] + (1 if newer or advert_at is None else 0),
                "is_contact": old["is_contact"],
            }
            self._mc_write_node(row)

    def mc_record_contact(self, c, now=None):
        """An entry from the radio's contact list (the library's contact dict). Its last_advert is
        the sender's own clock, so it only counts as 'heard' when it isn't in the future."""
        now = now or time.time()
        key = c["public_key"]
        lat, lon = c.get("adv_lat"), c.get("adv_lon")
        has_position = lat is not None and lon is not None and (lat, lon) != (0, 0)
        advert_at = c.get("last_advert") or None
        with self._lock, self._conn:
            old = self._conn.execute("SELECT * FROM mc_nodes WHERE public_key = ?", (key,)).fetchone()
            old = dict(old) if old else {"first_seen": now, "adverts": 0}
            newer = advert_at is not None and (old.get("advert_at") is None or advert_at > old["advert_at"])
            sent = advert_at if advert_at and advert_at <= now + 600 else None
            row = {**{k: old.get(k) for k in ("heard_by_us", "direct_heard", "last_snr", "last_rssi", "min_hops",
                                              "last_hops")},
                   "public_key": key,
                   "name": c.get("adv_name") or old.get("name"),
                   "type": c.get("type") if c.get("type") is not None else old.get("type"),
                   "latitude": lat if has_position and (newer or old.get("latitude") is None) else old.get("latitude"),
                   "longitude": lon if has_position and (newer or old.get("longitude") is None) else old.get("longitude"),
                   "first_seen": old["first_seen"],
                   "last_heard": max(x for x in (old.get("last_heard"), sent, 0) if x is not None) or None,
                   "advert_at": advert_at if newer else old.get("advert_at"),
                   "adverts": old["adverts"],
                   "is_contact": 1}
            self._mc_write_node(row)

    def _mc_write_node(self, row):
        cols = ", ".join(row)
        self._conn.execute(
            f"""INSERT INTO mc_nodes ({cols}) VALUES ({", ".join("?" for _ in row)})
                ON CONFLICT (public_key) DO UPDATE SET
                {", ".join(f"{k} = excluded.{k}" for k in row if k != "public_key")}""",
            tuple(row.values()),
        )

    def mc_record_message(self, m, channel_name=None, now=None):
        """A message the radio received (the library's CHANNEL_MSG_RECV / CONTACT_MSG_RECV payload).
        Channel messages carry the sender only as a "Name: " at the start of the text."""
        now = now or time.time()
        text = m.get("text") or ""
        sender = None
        is_channel = m.get("type") == "CHAN"
        if is_channel and ": " in text:
            sender, text = text.split(": ", 1)
        path_len = m.get("path_len")
        with self._lock, self._conn:
            return self._conn.execute(
                """INSERT INTO mc_messages (logged_at, direction, channel, channel_name, sender, pubkey_prefix, text,
                                            sent_at, hops, snr)
                   VALUES (?, 'in', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, m.get("channel_idx") if is_channel else None, channel_name if is_channel else None, sender,
                 None if is_channel else m.get("pubkey_prefix"), text, m.get("sender_timestamp"),
                 None if path_len in (None, 255) else path_len, m.get("SNR")),
            ).lastrowid

    def mc_conversations(self):
        """MeshCore channels and direct-message senders that have messages, newest activity first.
        A direct message's sender is known by the first 6 bytes of its public key."""
        return self._query(
            """SELECT CASE WHEN channel IS NULL THEN 'mc_dm' ELSE 'mc_channel' END AS kind,
                      COALESCE(channel, pubkey_prefix) AS key, MAX(channel_name) AS channel_name,
                      MAX(id) AS last_id, MAX(logged_at) AS last_at
               FROM mc_messages GROUP BY kind, key ORDER BY last_at DESC""")

    def mc_thread(self, channel=None, prefix=None, limit=500):
        """One MeshCore conversation, oldest first: a channel slot, or a direct-message sender's key prefix."""
        if prefix is not None:
            where, params = "m.channel IS NULL AND m.pubkey_prefix = ?", [prefix]
        else:
            where, params = "m.channel = ?", [channel or 0]
        rows = self._query(
            f"""SELECT m.*, (SELECT reason FROM alerts a WHERE a.mc_message = m.id LIMIT 1) AS alert_reason
                FROM mc_messages m WHERE {where} ORDER BY m.id DESC LIMIT ?""", [*params, limit])
        return list(reversed(rows))

    def mc_unread_count(self, channel=None, prefix=None, after_id=0):
        if prefix is not None:
            where, params = "channel IS NULL AND pubkey_prefix = ?", [prefix]
        else:
            where, params = "channel = ?", [channel or 0]
        return self._query(f"SELECT COUNT(*) FROM mc_messages WHERE {where} AND direction = 'in' AND id > ?",
                           [*params, after_id])[0][0]

    def mc_nodes(self, since=None):
        if since is None:
            return self._query("SELECT * FROM mc_nodes ORDER BY last_heard DESC NULLS LAST")
        return self._query("SELECT * FROM mc_nodes WHERE last_heard >= ? ORDER BY last_heard DESC", (since,))

    def mc_message(self, message_id):
        rows = self._query("SELECT * FROM mc_messages WHERE id = ?", (message_id,))
        return rows[0] if rows else None

    def mc_node_by_prefix(self, prefix):
        rows = self._query("SELECT * FROM mc_nodes WHERE public_key LIKE ? LIMIT 2", (prefix.lower() + "%",))
        return rows[0] if len(rows) == 1 else None

    def mc_messages(self, limit=100, since=None, channel=None):
        where, params = ["logged_at >= ?"], [since or 0]
        if channel is not None:
            where.append("channel = ?")
            params.append(channel)
        return self._query(
            f"SELECT * FROM mc_messages WHERE {' AND '.join(where)} ORDER BY logged_at DESC LIMIT ?",
            (*params, limit))

    def mc_packets(self, limit=100, since=None):
        return self._query("SELECT * FROM mc_packets WHERE logged_at >= ? ORDER BY logged_at DESC LIMIT ?",
                           (since or 0, limit))

    def mc_stats(self, since=None):
        since = since or 0
        by_type = self._query(
            """SELECT COALESCE(payload_type, '?') AS payload_type, COUNT(*) AS c,
                      SUM(CASE WHEN hops = 0 THEN 1 ELSE 0 END) AS direct
               FROM mc_packets WHERE logged_at >= ? GROUP BY payload_type ORDER BY c DESC""", (since,))
        nodes = self._query("SELECT COUNT(*) AS c FROM mc_nodes WHERE last_heard >= ?", (since,))[0]["c"]
        return {"packets": sum(r["c"] for r in by_type), "nodes heard": nodes,
                "messages": self._query("SELECT COUNT(*) AS c FROM mc_messages WHERE logged_at >= ?", (since,))[0]["c"]}, by_type
