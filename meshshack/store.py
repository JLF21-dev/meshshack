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

-- Small facts about this station that the radio doesn't hand back, e.g. the exact fixed
-- position (the radio only reports it rounded to the channel's position precision).
CREATE TABLE IF NOT EXISTS station (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
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

SCHEMA_VERSION = 5
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

            if portnum == "TEXT_MESSAGE_APP":
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
                        decoded.get("text"),
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
                       p.rx_snr, p.rx_rssi, p.hop_start - p.hop_limit AS hops, p.via_mqtt
                FROM messages m
                LEFT JOIN nodes n ON n.num = m.from_num
                LEFT JOIN packets p ON p.id = m.packet_row
                WHERE {where} ORDER BY m.id DESC LIMIT ?""",
            [*params, limit],
        )
        return list(reversed(rows))

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
        if not set(scopes) <= {"read", "send"} or "read" not in scopes:
            raise ValueError("scopes must be read, or read and send")
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
