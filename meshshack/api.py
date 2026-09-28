"""Localhost HTTP API: the desktop app, and other apps you allow, use the radio the logger holds.

Only one process can own the USB port, so the logger is the hub. Bound to 127.0.0.1.
- The desktop app authenticates with the token in hub.json (mode 0600, next to the database).
  It is the owner: it alone may change the radio's config or flip the transmit kill switch.
- Other apps use tokens from `meshshack token create`, scoped to read, or read and send. Their
  sends are budgeted per token by the airtime gatekeeper (airtime.py).
- GET /api/events is a server-sent event stream of packets, messages and connection changes.
Endpoints are listed in _routes() and in the README.
"""

import base64
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.protobuf import json_format
from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

from .airtime import Gatekeeper, Refused
from .store import BROADCAST_NUM, path_kind

log = logging.getLogger("meshshack")

MAX_TEXT_BYTES = 200  # what the official apps allow; the hard payload limit is a little higher
MAX_LONG_NAME = 39
MAX_SHORT_NAME = 4
MAX_CHANNEL_NAME = 11  # bytes, the firmware's limit
PRIMARY_LOCKED = ("The primary channel's name and key are what put you on the public mesh; "
                  "changing them here isn't allowed. Only its position sharing can be changed.")

Role = config_pb2.Config.DeviceConfig.Role
GpsMode = config_pb2.Config.PositionConfig.GpsMode
# Roles offered in the UI. ROUTER_CLIENT and REPEATER are deprecated; TAK/LOST_AND_FOUND are niche.
ROLES = ["CLIENT", "CLIENT_MUTE", "CLIENT_HIDDEN", "CLIENT_BASE", "TRACKER", "SENSOR", "ROUTER", "ROUTER_LATE"]


def key_kind(psk):
    """How a channel is encrypted, from its PSK: an empty key (or 0) means none; 1 byte (1-10)
    selects one of the well-known default keys, which everyone has; 16/32 bytes is a private key."""
    if not psk or psk == b"\x00":
        return "none"
    if len(psk) == 1:
        return "default"
    return f"private-{len(psk) * 8}"


def _psk(key):
    if key == "random":
        return secrets.token_bytes(32)
    if key == "default":
        return b"\x01"
    if key == "none":
        return b""
    try:
        psk = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        raise ApiError(400, "key must be random, default, none, or a base64 key")
    if len(psk) not in (16, 32):
        raise ApiError(400, "a key must be 16 or 32 bytes (AES-128 or AES-256)")
    return psk


def _channel_name(name):
    name = (name or "").strip() if isinstance(name, str) else ""
    if not name:
        raise ApiError(400, "a channel needs a name")
    if len(name.encode("utf-8")) > MAX_CHANNEL_NAME:
        raise ApiError(400, f"channel names are at most {MAX_CHANNEL_NAME} bytes")
    return name


def _precision(value):
    if not isinstance(value, int) or not 0 <= value <= 32:
        raise ApiError(400, "position_precision must be 0 (don't share) to 32 (exact)")
    return value


def _with_warning(result, warning):
    if warning:
        result["warning"] = warning
    return result


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def preset_display_name(preset):
    """'LONG_FAST' -> 'LongFast', which is what apps show for an unnamed primary channel."""
    return "".join(word.capitalize() for word in preset.split("_"))


def parse_node(value):
    """Accept a node number or a '!a1b2c3d4' ID from the client."""
    if isinstance(value, int) and 0 < value < BROADCAST_NUM:
        return value
    if isinstance(value, str) and value.startswith("!"):
        try:
            return int(value[1:], 16)
        except ValueError:
            pass
    raise ApiError(400, f"bad node: {value!r}")


class Radio:
    """Commands against the collector's live interface. Every send returns immediately;
    replies arrive as packets the collector logs."""

    def __init__(self, collector, store, gate=None):
        self.collector = collector
        self.store = store
        self.gate = gate or Gatekeeper(store, metrics=self._metrics)
        self._lock = threading.Lock()  # one command on the serial link at a time

    def _metrics(self):
        iface = self.collector.iface
        if iface is None or not iface.isConnected.is_set():
            return {}
        return (iface.getMyNodeInfo() or {}).get("deviceMetrics") or {}

    def _iface(self):
        iface = self.collector.iface
        if iface is None or not iface.isConnected.is_set():
            raise ApiError(503, "radio not connected")
        return iface

    def _run(self, kind, fn, to=None, channel=None, source="manual", allow_broadcast=False):
        """The one way anything reaches the radio: the airtime gatekeeper authorizes (and logs)
        it first, under the same lock as the send, so two requests can't both slip through.
        Returns (result, warning)."""
        with self._lock:
            try:
                ticket = self.gate.authorize(source, kind, to=to, channel=channel, allow_broadcast=allow_broadcast)
            except Refused as ex:
                raise ApiError(ex.status, str(ex)) from ex
            try:
                result = fn()
            except ApiError:
                raise
            except (Exception, SystemExit) as ex:  # the library sys.exit()s on some errors
                log.exception("Radio command failed")
                raise ApiError(500, f"radio command failed: {ex}") from ex
            if getattr(result, "id", None) is not None:
                self.gate.sent(ticket, result.id)
            return result, ticket.warning

    def _my_user(self, iface):
        me = iface.getMyNodeInfo() or {}
        return me.get("user") or {}

    # ---- status ----

    def status(self):
        iface = self.collector.iface
        if iface is None or not iface.isConnected.is_set():
            return {"connected": False}
        node = iface.localNode
        cfg = node.localConfig
        me = iface.getMyNodeInfo() or {}
        user = me.get("user") or {}
        preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(cfg.lora.modem_preset)
        channels = []
        for ch in node.channels or []:
            if ch.role == channel_pb2.Channel.Role.DISABLED:
                continue
            name = ch.settings.name
            if not name:
                name = preset_display_name(preset) if ch.role == channel_pb2.Channel.Role.PRIMARY else f"Channel {ch.index}"
            channels.append({"index": ch.index, "name": name, "role": channel_pb2.Channel.Role.Name(ch.role)})
        position = me.get("position") or {}
        return {
            "connected": True,
            "port": self.collector.connected_port,
            "node": {
                "num": iface.myInfo.my_node_num,
                "id": user.get("id"),
                "long_name": user.get("longName"),
                "short_name": user.get("shortName"),
                "hw_model": user.get("hwModel"),
                "is_licensed": bool(user.get("isLicensed")),
                "firmware": iface.metadata.firmware_version if iface.metadata else None,
            },
            "metrics": me.get("deviceMetrics") or {},
            "lora": {
                "region": config_pb2.Config.LoRaConfig.RegionCode.Name(cfg.lora.region),
                "modem_preset": preset,
                "hop_limit": cfg.lora.hop_limit,
                "tx_enabled": cfg.lora.tx_enabled,
            },
            "device": {"role": Role.Name(cfg.device.role)},
            "position": {
                "broadcast_secs": cfg.position.position_broadcast_secs,
                "smart_enabled": cfg.position.position_broadcast_smart_enabled,
                "fixed_position": cfg.position.fixed_position,
                "gps_mode": GpsMode.Name(cfg.position.gps_mode),
                "latitude": position.get("latitude"),
                "longitude": position.get("longitude"),
                "altitude": position.get("altitude"),
            },
            "channels": channels,
            "roles": ROLES,
            "gps_modes": list(GpsMode.keys()),
        }

    # ---- messaging and requests ----

    def send_text(self, text, channel=0, to=None, reply_id=None, source="manual", allow_broadcast=True):
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "empty message")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ApiError(400, f"message longer than {MAX_TEXT_BYTES} bytes")
        dest = parse_node(to) if to is not None else BROADCAST_NUM
        iface = self._iface()
        if dest == BROADCAST_NUM:
            valid = {c.index for c in iface.localNode.channels or [] if c.role != channel_pb2.Channel.Role.DISABLED}
            if channel not in valid:
                raise ApiError(400, f"no channel {channel}")

        def send():
            return iface.sendData(
                text.encode("utf-8"),
                destinationId=dest,
                portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                wantAck=True,
                channelIndex=channel if dest == BROADCAST_NUM else 0,
                replyId=reply_id,
            )

        kind = "broadcast" if dest == BROADCAST_NUM else "dm"
        pkt, warning = self._run(kind, send, to=None if dest == BROADCAST_NUM else dest,
                                 channel=channel if dest == BROADCAST_NUM else None,
                                 source=source, allow_broadcast=allow_broadcast)
        row = self.store.record_outgoing_message(
            pkt.id, iface.myInfo.my_node_num, dest, channel if dest == BROADCAST_NUM else 0, text, reply_id
        )
        return _with_warning({"packet_id": pkt.id, "message_row": row}, warning)

    def traceroute(self, to, source="manual"):
        dest = parse_node(to)
        iface = self._iface()
        hop_limit = iface.localNode.localConfig.lora.hop_limit or 3
        pkt, warning = self._run("traceroute", lambda: iface.sendData(
            mesh_pb2.RouteDiscovery(),
            destinationId=dest,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantResponse=True,
            hopLimit=hop_limit,
        ), to=dest, source=source)
        self.store.record_request(pkt.id, "traceroute", dest)
        return _with_warning({"packet_id": pkt.id}, warning)

    def request(self, to, what, source="manual"):
        dest = parse_node(to)
        iface = self._iface()
        if what == "position":
            payload, port = mesh_pb2.Position(), portnums_pb2.PortNum.POSITION_APP
        elif what == "telemetry":
            payload = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics())
            port = portnums_pb2.PortNum.TELEMETRY_APP
        elif what == "nodeinfo":
            # Asking for node info is done by sending ours with want_response.
            payload = self._user_protobuf(iface)
            port = portnums_pb2.PortNum.NODEINFO_APP
        else:
            raise ApiError(400, f"can't request {what!r}")
        pkt, warning = self._run(
            "request", lambda: iface.sendData(payload, destinationId=dest, portNum=port, wantResponse=True), to=dest,
            source=source,
        )
        self.store.record_request(pkt.id, what, dest)
        return _with_warning({"packet_id": pkt.id}, warning)

    def announce(self, source="manual", allow_broadcast=True):
        """Broadcast our node info so others learn/refresh our name."""
        iface = self._iface()
        payload = self._user_protobuf(iface)
        pkt, warning = self._run("announce", lambda: iface.sendData(payload, portNum=portnums_pb2.PortNum.NODEINFO_APP),
                                 source=source, allow_broadcast=allow_broadcast)
        return _with_warning({"packet_id": pkt.id}, warning)

    def _user_protobuf(self, iface):
        user = {k: v for k, v in self._my_user(iface).items() if k != "raw"}
        return json_format.ParseDict(user, mesh_pb2.User(), ignore_unknown_fields=True)

    # ---- favorites and ignored nodes (stored on this radio; nothing is transmitted) ----

    def set_favorite(self, node, favorite):
        return self._node_flag(node, favorite, "favorite", "setFavorite", "removeFavorite", "isFavorite")

    def set_ignored(self, node, ignored):
        return self._node_flag(node, ignored, "ignored", "setIgnored", "removeIgnored", "isIgnored")

    def _node_flag(self, node, on, name, set_fn, remove_fn, key):
        num = parse_node(node)
        if not isinstance(on, bool):
            raise ApiError(400, f"{name} must be true or false")
        iface = self._iface()
        if num == iface.myInfo.my_node_num:
            raise ApiError(400, "that's this radio")
        self._run("setting", lambda: getattr(iface.localNode, set_fn if on else remove_fn)(num), to=num)
        known = (iface.nodesByNum or {}).get(num) if hasattr(iface, "nodesByNum") else None
        if known is not None:
            known[key] = on  # the library's copy only refreshes on the radio's next report
        self.store.set_node_flags(num, **{name: on})
        self.store.record_event("command", f"{'' if on else 'un'}{'favorite' if name == 'favorite' else 'ignore'} "
                                           f"!{num:08x}")
        return {"ok": True}

    # ---- channels (owner only: these include the encryption keys) ----

    def channels(self):
        iface = self._iface()
        lat = ((iface.getMyNodeInfo() or {}).get("position") or {}).get("latitude")
        out = []
        for ch in iface.localNode.channels or []:
            if ch.role == channel_pb2.Channel.Role.DISABLED:
                continue
            s = ch.settings
            out.append({
                "index": ch.index,
                "name": s.name,
                "role": channel_pb2.Channel.Role.Name(ch.role),
                "encryption": key_kind(s.psk),
                "psk": base64.b64encode(s.psk).decode(),
                "position_precision": s.module_settings.position_precision,
                "share_url": self._share_url(iface, ch),
            })
        return {"channels": out, "latitude": lat}

    @staticmethod
    def _share_url(iface, ch):
        """A link (and QR) that adds just this channel, with the LoRa settings it needs, to another
        node. The "?add=true" form asks the receiving app to add rather than replace its channels."""
        channel_set = apponly_pb2.ChannelSet()
        channel_set.settings.append(ch.settings)
        channel_set.lora_config.CopyFrom(iface.localNode.localConfig.lora)
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode().rstrip("=")
        return f"https://meshtastic.org/e/?add=true#{encoded}"

    def add_channel(self, name, key="random", position_precision=None):
        iface = self._iface()
        name = _channel_name(name)
        node = iface.localNode
        if any(c.settings.name == name for c in node.channels or [] if c.role != channel_pb2.Channel.Role.DISABLED):
            raise ApiError(400, f"there's already a channel named {name!r}")
        slot = node.getDisabledChannel()
        if slot is None:
            raise ApiError(400, "all 8 channel slots are in use")
        psk = _psk(key)
        precision = _precision(position_precision) if position_precision is not None else None

        def write():
            slot.role = channel_pb2.Channel.Role.SECONDARY
            slot.settings.name = name
            slot.settings.psk = psk
            if precision is not None:
                slot.settings.module_settings.position_precision = precision
            node.writeChannel(slot.index)

        self._run("config", write)
        self.store.record_event("command", f"add channel {slot.index} {name!r} ({key_kind(psk)})")
        return {"index": slot.index}

    def update_channel(self, index, name=None, key=None, position_precision=None):
        iface = self._iface()
        ch = self._channel(iface, index)
        primary = ch.role == channel_pb2.Channel.Role.PRIMARY
        if primary and (name is not None or key is not None):
            raise ApiError(403, PRIMARY_LOCKED)
        new_name = _channel_name(name) if name is not None else None
        psk = _psk(key) if key is not None else None
        precision = _precision(position_precision) if position_precision is not None else None

        def write():
            if new_name is not None:
                ch.settings.name = new_name
            if psk is not None:
                ch.settings.psk = psk
            if precision is not None:
                ch.settings.module_settings.position_precision = precision
            iface.localNode.writeChannel(ch.index)

        self._run("config", write)
        self.store.record_event("command", f"update channel {index}")
        return {"ok": True}

    def delete_channel(self, index):
        iface = self._iface()
        ch = self._channel(iface, index)
        if ch.role != channel_pb2.Channel.Role.SECONDARY:
            raise ApiError(403, "only secondary channels can be deleted")
        name = ch.settings.name
        self._run("config", lambda: iface.localNode.deleteChannel(index))
        self.store.record_event("command", f"delete channel {index} {name!r}")
        return {"ok": True}

    @staticmethod
    def _channel(iface, index):
        if not isinstance(index, int):
            raise ApiError(400, "index must be a number")
        for ch in iface.localNode.channels or []:
            if ch.index == index and ch.role != channel_pb2.Channel.Role.DISABLED:
                return ch
        raise ApiError(404, f"no channel {index}")

    # ---- transmit control ----

    def tx_status(self, limit=20):
        return {
            "enabled": self.gate.transmit_enabled(),
            "log": [dict(r) for r in self.store.tx_log(limit=limit)],
        }

    def set_transmit(self, enabled):
        if not isinstance(enabled, bool):
            raise ApiError(400, "enabled must be true or false")
        self.gate.set_transmit_enabled(enabled, by="api")
        return {"enabled": enabled}

    # ---- device control ----

    def reboot(self):
        iface = self._iface()
        self._run("config", lambda: iface.localNode.reboot(secs=5))
        self.store.record_event("command", "reboot")
        return {"ok": True}

    def set_owner(self, long_name, short_name):
        long_name = (long_name or "").strip()
        short_name = (short_name or "").strip()
        if not long_name or not short_name:
            raise ApiError(400, "long and short name are required")
        if len(long_name.encode("utf-8")) > MAX_LONG_NAME:
            raise ApiError(400, f"long name longer than {MAX_LONG_NAME} bytes")
        if len(short_name) > MAX_SHORT_NAME:
            raise ApiError(400, f"short name longer than {MAX_SHORT_NAME} characters")
        iface = self._iface()
        # setOwner() clears the licensed (ham) flag unless it's passed back in.
        licensed = bool(self._my_user(iface).get("isLicensed"))
        self._run("config", lambda: iface.localNode.setOwner(long_name, short_name, is_licensed=licensed))
        # The library's copy of our node only updates when the radio next sends node info;
        # update it now so status (and the GUI's form) reflect the change immediately.
        user = self._my_user(iface)
        user["longName"], user["shortName"] = long_name, short_name
        self.store.record_event("command", f"set owner {long_name!r} / {short_name!r}")
        return {"ok": True}

    def set_role(self, role):
        if role not in ROLES:
            raise ApiError(400, f"role must be one of {', '.join(ROLES)}")
        iface = self._iface()

        def write():
            node = iface.localNode
            node.localConfig.device.role = Role.Value(role)
            node.writeConfig("device")

        self._run("config", write)
        self.store.record_event("command", f"set role {role}")
        return {"ok": True}

    def set_position(self, broadcast_secs, smart_enabled, gps_mode, fixed=None):
        """fixed: None leaves fixed position alone, False turns it off,
        {"latitude", "longitude", "altitude"} sets it."""
        if not isinstance(broadcast_secs, int) or broadcast_secs < 0:
            raise ApiError(400, "broadcast_secs must be a non-negative integer")
        if gps_mode not in GpsMode.keys():
            raise ApiError(400, f"gps_mode must be one of {', '.join(GpsMode.keys())}")
        if isinstance(fixed, dict):
            try:
                lat, lon = float(fixed["latitude"]), float(fixed["longitude"])
                alt = int(fixed.get("altitude") or 0)
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, "fixed position needs numeric latitude and longitude")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                raise ApiError(400, "fixed position out of range")
        elif fixed not in (None, False):
            raise ApiError(400, "fixed must be null, false, or a position")
        iface = self._iface()

        def write():
            node = iface.localNode
            pos = node.localConfig.position
            was_fixed = pos.fixed_position
            # One transaction so the radio applies (and possibly reboots) once.
            node.beginSettingsTransaction()
            if fixed is False and was_fixed:
                node.removeFixedPosition()
            pos.position_broadcast_secs = broadcast_secs
            pos.position_broadcast_smart_enabled = bool(smart_enabled)
            pos.gps_mode = GpsMode.Value(gps_mode)
            if fixed is False:
                pos.fixed_position = False
            elif isinstance(fixed, dict):
                pos.fixed_position = True
            node.writeConfig("position")
            if isinstance(fixed, dict):
                node.setFixedPosition(lat, lon, alt)
            node.commitSettingsTransaction()

        self._run("config", write)
        # The radio reports its position rounded to the channel's precision, so keep what was entered.
        if isinstance(fixed, dict):
            self.store.set_station("fixed_position", {"latitude": lat, "longitude": lon, "altitude": alt})
        elif fixed is False:
            self.store.set_station("fixed_position", None)
        self.store.record_event("command", f"set position config (fixed={fixed})")
        return {"ok": True}


MAX_LIMIT = 1000
MAX_STREAMS = 8
STREAM_KEEPALIVE = 15  # seconds between keep-alive comments on a quiet event stream


@dataclass
class Caller:
    """Who is calling. The app (the token in hub.json) is the owner: it's "you", may change the
    radio's config and flip the kill switch. Other apps use tokens from `meshshack token`."""

    source: str  # "manual" for the owner, else "api:<token name>"
    scopes: frozenset
    allow_broadcast: bool


def parse_since(value):
    """A unix time, or a duration back from now like '30m', '24h', '7d'."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+)([smhd])", value.strip().lower())
    if not m:
        raise ApiError(400, f"since: expected a unix time or a duration like 24h, got {value!r}")
    return time.time() - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Query:
    def __init__(self, raw):
        self.params = {k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()}

    def str(self, name):
        return self.params.get(name)

    def limit(self, default=100):
        try:
            value = int(self.params.get("limit", default))
        except ValueError:
            raise ApiError(400, "limit must be a number")
        return max(1, min(value, MAX_LIMIT))

    def since(self):
        return parse_since(self.params.get("since"))

    def int(self, name):
        if name not in self.params:
            return None
        try:
            return int(self.params[name])
        except ValueError:
            raise ApiError(400, f"{name} must be a number")

    def node(self, name="node"):
        return parse_node(_maybe_int(self.params[name])) if name in self.params else None


def _maybe_int(value):
    try:
        return int(value)
    except ValueError:
        return value


def _rows(rows, json_field=None, parse=()):
    """sqlite rows -> JSON-able dicts; json_field is replaced by its parsed content."""
    out = []
    for r in rows:
        d = dict(r)
        for field in parse:
            if d.get(field) is not None:
                d[field] = json.loads(d[field])
        if json_field:
            d["packet"] = json.loads(d.pop(json_field))
        out.append(d)
    return out


def _routes(radio):
    store = radio.store

    def body_args(body, *required, optional=()):
        missing = [k for k in required if k not in body]
        if missing:
            raise ApiError(400, f"missing {', '.join(missing)}")
        return {k: body[k] for k in (*required, *optional) if k in body}

    def nodes(c, b, q):
        via = store.heard_via()
        out = []
        for n in store.nodes(since=q.since())[: q.limit(500)]:
            d = dict(n)
            d["via"] = path_kind(*via.get(n["num"], (0, 0)))
            out.append(d)
        return {"nodes": out}

    def messages(c, b, q):
        peer, channel = q.node("peer"), q.int("channel")
        if peer is not None or channel is not None:
            rows = store.thread(channel=channel, peer=peer, limit=q.limit())
        else:
            rows = store.messages(limit=q.limit(), since=q.since())
        return {"messages": _rows(rows)}

    def send(c, b, q):
        args = body_args(b, "text", optional=("channel", "to", "reply_id"))
        return radio.send_text(**args, source=c.source, allow_broadcast=c.allow_broadcast)

    # (method, path) -> (scope needed, handler(caller, body, query))
    return {
        ("GET", "/api/status"): ("read", lambda c, b, q: radio.status()),
        ("GET", "/api/nodes"): ("read", nodes),
        ("GET", "/api/messages"): ("read", messages),
        ("GET", "/api/packets"): ("read", lambda c, b, q: {"packets": _rows(
            store.packets(limit=q.limit(), portnum=q.str("type"), since=q.since(), from_num=q.node()), json_field="json")}),
        ("GET", "/api/telemetry"): ("read", lambda c, b, q: {"telemetry": _rows(
            store.telemetry(limit=q.limit(), kind=q.str("kind"), since=q.since(), from_num=q.node()), parse=("metrics",))}),
        ("GET", "/api/positions"): ("read", lambda c, b, q: {"positions": _rows(
            store.positions(limit=q.limit(500), since=q.since(), from_num=q.node()))}),
        ("GET", "/api/requests"): ("read", lambda c, b, q: {"requests": _rows(
            store.requests(limit=q.limit()), parse=("response_json",))}),
        ("GET", "/api/tx"): ("read", lambda c, b, q: radio.tx_status(limit=q.limit(20))),
        ("POST", "/api/tx"): ("owner", lambda c, b, q: radio.set_transmit(**body_args(b, "enabled"))),
        ("POST", "/api/send"): ("send", send),
        ("POST", "/api/traceroute"): ("send", lambda c, b, q: radio.traceroute(**body_args(b, "to"), source=c.source)),
        ("POST", "/api/request"): ("send", lambda c, b, q: radio.request(**body_args(b, "to", "what"), source=c.source)),
        ("POST", "/api/announce"): ("send", lambda c, b, q: radio.announce(source=c.source, allow_broadcast=c.allow_broadcast)),
        ("POST", "/api/reboot"): ("owner", lambda c, b, q: radio.reboot()),
        ("GET", "/api/channels"): ("owner", lambda c, b, q: radio.channels()),
        ("POST", "/api/nodes/favorite"): ("owner", lambda c, b, q: radio.set_favorite(**body_args(b, "node", "favorite"))),
        ("POST", "/api/nodes/ignore"): ("owner", lambda c, b, q: radio.set_ignored(**body_args(b, "node", "ignored"))),
        ("POST", "/api/channels/add"): ("owner", lambda c, b, q: radio.add_channel(
            **body_args(b, "name", optional=("key", "position_precision")))),
        ("POST", "/api/channels/update"): ("owner", lambda c, b, q: radio.update_channel(
            **body_args(b, "index", optional=("name", "key", "position_precision")))),
        ("POST", "/api/channels/delete"): ("owner", lambda c, b, q: radio.delete_channel(**body_args(b, "index"))),
        ("POST", "/api/config/owner"): ("owner", lambda c, b, q: radio.set_owner(**body_args(b, "long_name", "short_name"))),
        ("POST", "/api/config/role"): ("owner", lambda c, b, q: radio.set_role(**body_args(b, "role"))),
        ("POST", "/api/config/position"): ("owner", lambda c, b, q: radio.set_position(
            **body_args(b, "broadcast_secs", "smart_enabled", "gps_mode", optional=("fixed",))
        )),
    }


class ApiServer:
    def __init__(self, radio, info_path, port=8765, bus=None):
        self.info_path = info_path
        self.token = secrets.token_urlsafe(24)
        self.bus = bus
        self._stopping = threading.Event()
        routes = _routes(radio)
        store = radio.store
        owner_token = self.token
        server = self

        def authenticate(header):
            presented = header[len("Bearer "):] if header.startswith("Bearer ") else ""
            if presented and hmac.compare_digest(presented, owner_token):
                return Caller("manual", frozenset({"read", "send", "owner"}), True)
            row = store.token_for(presented) if presented else None
            if row is None:
                raise ApiError(401, "bad or missing token")
            return Caller(f"api:{row['name']}", frozenset(row["scopes"].split(",")), bool(row["allow_broadcast"]))

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, result):
                data = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                try:
                    caller = authenticate(self.headers.get("Authorization", ""))
                    path, _, raw_query = self.path.partition("?")
                    if method == "GET" and path == "/api/events":
                        if "read" not in caller.scopes:
                            raise ApiError(403, "this token can't read")
                        return self._stream()
                    entry = routes.get((method, path))
                    if entry is None:
                        raise ApiError(404, "no such endpoint")
                    scope, handler = entry
                    if scope not in caller.scopes:
                        raise ApiError(403, "only the MeshShack app can do that" if scope == "owner"
                                       else f"this token doesn't have the {scope!r} scope")
                    body = {}
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        try:
                            body = json.loads(self.rfile.read(length))
                        except ValueError:
                            raise ApiError(400, "body is not JSON")
                        if not isinstance(body, dict):
                            raise ApiError(400, "body must be a JSON object")
                    status, result = 200, handler(caller, body, Query(raw_query))
                except ApiError as ex:
                    status, result = ex.status, {"error": str(ex)}
                except Exception as ex:
                    log.exception("API error on %s %s", method, self.path)
                    status, result = 500, {"error": str(ex)}
                self._reply(status, result)

            def _stream(self):
                """Server-sent events: every packet as it's logged, text messages, connection changes."""
                if server.bus is None:
                    raise ApiError(503, "no event stream in this process")
                if server.bus.count() >= MAX_STREAMS:
                    raise ApiError(503, f"at most {MAX_STREAMS} event streams at once")
                sub = server.bus.subscribe()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not server._stopping.is_set():
                        event = sub.get(timeout=STREAM_KEEPALIVE)
                        if event is None:
                            self.wfile.write(b": keep-alive\n\n")
                        else:
                            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # the client went away
                finally:
                    server.bus.unsubscribe(sub)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, fmt, *args):
                log.debug("api: " + fmt, *args)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, name="meshshack-api", daemon=True).start()
        # Tell local clients where we are and how to authenticate; owner-only permissions.
        fd = os.open(self.info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # in case an older file had looser permissions
        with os.fdopen(fd, "w") as f:
            json.dump({"url": self.url, "token": self.token, "pid": os.getpid()}, f)
        log.info("API listening on %s", self.url)

    def stop(self):
        self._stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            os.unlink(self.info_path)
        except FileNotFoundError:
            pass
