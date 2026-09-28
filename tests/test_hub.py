"""Outgoing messages, delivery tracking, schema migration, and the localhost API."""

import json
import os
import sqlite3
import stat
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2, mesh_pb2

from meshshack.api import ApiServer, Radio
from meshshack.store import BROADCAST_NUM, SCHEMA, Store

ME = 0xA1B2C3D4
BOB = 0x0BADBEEF


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def ack(request_id, sender, error=None):
    routing = {"errorReason": error} if error else {}
    return {"from": sender, "to": ME, "id": 999, "decoded": {"portnum": "ROUTING_APP", "requestId": request_id, "routing": routing}}


# ---- delivery status ------------------------------------------------------


def status_of(store, row):
    return store._query("SELECT status, status_detail FROM messages WHERE id = ?", (row,))[0]


def test_dm_relayed_then_delivered(store):
    row = store.record_outgoing_message(111, ME, BOB, 0, "hi bob")
    assert status_of(store, row)["status"] == "sending"
    store.record_packet(ack(111, ME))  # implicit ack: a neighbor rebroadcast it
    assert status_of(store, row)["status"] == "relayed"
    store.record_packet(ack(111, BOB))  # real ack from the destination
    assert status_of(store, row)["status"] == "delivered"
    store.record_packet(ack(111, ME, "MAX_RETRANSMIT"))  # a late NAK doesn't undo it
    assert status_of(store, row)["status"] == "delivered"


def test_broadcast_failure(store):
    row = store.record_outgoing_message(222, ME, BROADCAST_NUM, 0, "anyone?")
    store.record_packet(ack(222, ME, "MAX_RETRANSMIT"))
    assert tuple(status_of(store, row)) == ("failed", "MAX_RETRANSMIT")


def test_ack_arriving_before_send_is_recorded(store):
    store.record_packet(ack(333, ME, "NO_CHANNEL"))
    row = store.record_outgoing_message(333, ME, BROADCAST_NUM, 0, "fast nak")
    assert tuple(status_of(store, row)) == ("failed", "NO_CHANNEL")


def test_conversations_and_threads(store):
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 1, "channel": 0,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "on ch0"}})
    store.record_packet({"from": BOB, "to": ME, "id": 2,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "dm to me"}})
    store.record_outgoing_message(3, ME, BOB, 0, "dm back")
    store.record_outgoing_message(4, ME, BROADCAST_NUM, 1, "on ch1")

    convs = {(c["kind"], c["key"]) for c in store.conversations()}
    assert convs == {("channel", 0), ("channel", 1), ("dm", BOB)}
    assert [m["text"] for m in store.thread(peer=BOB)] == ["dm to me", "dm back"]
    assert [m["text"] for m in store.thread(channel=0)] == ["on ch0"]


def test_request_replies_are_matched_by_the_hub(store):
    store.record_request(555, "traceroute", BOB)
    store.record_packet(ack(555, ME))  # relay ack: not an answer
    assert store.requests()[0]["status"] == "pending"
    store.record_packet({"from": BOB, "to": ME, "id": 7,
                         "decoded": {"portnum": "TRACEROUTE_APP", "requestId": 555,
                                     "traceroute": {"route": [], "snrTowards": [24]}}})
    [req] = store.requests()
    assert req["status"] == "answered" and '"TRACEROUTE_APP"' in req["response_json"]
    # A late routing error doesn't undo a real reply.
    store.record_packet({"from": ME, "to": ME, "id": 8, "decoded": {
        "portnum": "ROUTING_APP", "requestId": 555, "routing": {"errorReason": "NO_RESPONSE"}}})
    assert store.requests()[0]["status"] == "answered"


def test_migrates_v0_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # every other table is unchanged since v0
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, packet_row INTEGER NOT NULL REFERENCES packets (id),
            logged_at REAL NOT NULL, packet_id INTEGER, from_num INTEGER, from_id TEXT, to_num INTEGER,
            to_id TEXT, channel INTEGER, is_direct INTEGER NOT NULL, text TEXT, reply_id INTEGER, emoji INTEGER);
        CREATE INDEX messages_logged_at ON messages (logged_at);
        INSERT INTO packets (id, logged_at, json) VALUES (1, 100.0, '{}');
        INSERT INTO messages (packet_row, logged_at, from_num, channel, is_direct, text) VALUES (1, 100.0, 5, 0, 0, 'old msg');
    """)
    conn.close()

    s = Store(path)
    [msg] = s.thread(channel=0)
    assert msg["text"] == "old msg" and msg["direction"] == "in"
    s.record_outgoing_message(1, ME, BOB, 0, "no packet row needed now")
    s.close()
    Store(path).close()  # reopening an up-to-date database is a no-op


# ---- API ------------------------------------------------------------------


class FakeNode:
    def __init__(self):
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.hop_limit = 3
        self.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        self.channels = [
            channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY),
            channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.SECONDARY,
                                settings=channel_pb2.ChannelSettings(name="Admin")),
            channel_pb2.Channel(index=2, role=channel_pb2.Channel.Role.DISABLED),
        ]
        self.calls = []

    def getDisabledChannel(self):
        return next((c for c in self.channels if c.role == channel_pb2.Channel.Role.DISABLED), None)

    def __getattr__(self, name):  # record any admin call (setOwner, reboot, writeConfig, ...)
        return lambda *a, **kw: self.calls.append((name, a, kw))


class FakeInterface:
    def __init__(self):
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.myInfo = SimpleNamespace(my_node_num=ME)
        self.metadata = SimpleNamespace(firmware_version="2.7.15")
        self.localNode = FakeNode()
        self.sent = []

    def getMyNodeInfo(self):
        return {"user": {"id": "!a1b2c3d4", "longName": "N0CALL Base", "shortName": "N0C",
                         "hwModel": "HELTEC_V4", "isLicensed": True},
                "position": {"latitude": 40.1149, "longitude": -88.2281}}

    def sendData(self, data, destinationId=BROADCAST_NUM, **kw):
        self.sent.append((data, destinationId, kw))
        return mesh_pb2.MeshPacket(id=1000 + len(self.sent))

    def _generatePacketId(self):  # used for reactions, which sendData can't flag
        return 2000 + len(self.sent)

    def _sendPacket(self, packet, destinationId=BROADCAST_NUM, **kw):
        self.sent.append((packet, destinationId, kw))
        return packet


@pytest.fixture
def api(store, tmp_path):
    iface = FakeInterface()
    collector = SimpleNamespace(iface=iface, connected_port="/dev/ttyACM0")
    server = ApiServer(Radio(collector, store), tmp_path / "hub.json", port=0)
    server.start()
    info = json.loads((tmp_path / "hub.json").read_text())

    def call(method, path, body=None, token=info["token"]):
        req = urllib.request.Request(info["url"] + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    yield call, iface, collector
    server.stop()
    assert not (tmp_path / "hub.json").exists()


def test_hub_file_is_private(api, tmp_path):
    assert stat.S_IMODE(os.stat(tmp_path / "hub.json").st_mode) == 0o600


def test_requires_token(api):
    call, _, _ = api
    assert call("GET", "/api/status", token="wrong")[0] == 401


def test_status(api):
    call, _, _ = api
    code, status = call("GET", "/api/status")
    assert code == 200 and status["connected"]
    assert status["node"]["short_name"] == "N0C" and status["node"]["firmware"] == "2.7.15"
    assert status["lora"]["region"] == "US"
    assert [(c["index"], c["name"]) for c in status["channels"]] == [(0, "LongFast"), (1, "Admin")]


def test_send_broadcast_and_dm(api, store):
    call, iface, _ = api
    code, result = call("POST", "/api/send", {"text": "73 de N0CALL", "channel": 1})
    assert code == 200
    data, dest, kw = iface.sent[0]
    assert data == b"73 de N0CALL" and dest == BROADCAST_NUM and kw["channelIndex"] == 1 and kw["wantAck"]

    assert call("POST", "/api/send", {"text": "hi bob", "to": "!0badbeef"})[0] == 200
    assert iface.sent[1][1] == BOB

    [dm] = store.thread(peer=BOB)
    assert dm["direction"] == "out" and dm["status"] == "sending" and dm["packet_id"] == 1002


def test_send_validation(api):
    call, iface, collector = api
    assert call("POST", "/api/send", {"text": "   "})[0] == 400
    assert call("POST", "/api/send", {"text": "x" * 201})[0] == 400
    assert call("POST", "/api/send", {"text": "hi", "channel": 2})[0] == 400  # disabled channel
    assert call("POST", "/api/send", {"text": "hi", "to": "bob"})[0] == 400
    assert call("POST", "/api/send", {})[0] == 400
    collector.iface = None
    assert call("POST", "/api/send", {"text": "hi"})[0] == 503
    assert call("GET", "/api/status")[1] == {"connected": False}
    assert iface.sent == []


def test_owner_keeps_licensed_flag(api):
    call, iface, _ = api
    assert call("POST", "/api/config/owner", {"long_name": "N0CALL Home", "short_name": "N0CA"})[0] == 200
    assert iface.localNode.calls == [("setOwner", ("N0CALL Home", "N0CA"), {"is_licensed": True})]
    assert call("POST", "/api/config/owner", {"long_name": "x", "short_name": "TOOLONG"})[0] == 400


def test_role_and_position(api, store):
    call, iface, _ = api
    assert call("POST", "/api/config/role", {"role": "REPEATER"})[0] == 400
    assert call("POST", "/api/config/role", {"role": "CLIENT_BASE"})[0] == 200
    assert iface.localNode.localConfig.device.role == config_pb2.Config.DeviceConfig.Role.CLIENT_BASE

    body = {"broadcast_secs": 900, "smart_enabled": False, "gps_mode": "NOT_PRESENT",
            "fixed": {"latitude": 40.1149, "longitude": -88.2281, "altitude": 160}}
    assert call("POST", "/api/config/position", body)[0] == 200
    pos = iface.localNode.localConfig.position
    assert pos.position_broadcast_secs == 900 and pos.fixed_position
    names = [c[0] for c in iface.localNode.calls]
    assert names[-4:] == ["beginSettingsTransaction", "writeConfig", "setFixedPosition", "commitSettingsTransaction"]

    # The exact position is kept, since the radio only reports it rounded.
    assert store.station("fixed_position") == {"latitude": 40.1149, "longitude": -88.2281, "altitude": 160}

    body["fixed"] = {"latitude": 95, "longitude": 0}
    assert call("POST", "/api/config/position", body)[0] == 400


def test_requests(api):
    call, iface, _ = api
    assert call("POST", "/api/traceroute", {"to": BOB})[1]["packet_id"] == 1001
    assert call("POST", "/api/request", {"to": BOB, "what": "telemetry"})[0] == 200
    assert call("POST", "/api/request", {"to": BOB, "what": "nodeinfo"})[0] == 200
    user = iface.sent[2][0]
    assert isinstance(user, mesh_pb2.User) and user.long_name == "N0CALL Base" and user.is_licensed
    assert call("POST", "/api/request", {"to": BOB, "what": "secrets"})[0] == 400
    assert all(kw.get("wantResponse") for _, _, kw in iface.sent)


def test_kill_switch_over_the_api(api):
    call, iface, _ = api
    assert call("GET", "/api/tx")[1]["enabled"] is True
    assert call("POST", "/api/tx", {"enabled": False})[1] == {"enabled": False}
    before = len(iface.sent)
    status, body = call("POST", "/api/send", {"text": "hello", "to": BOB})
    assert status == 403 and "turned off" in body["error"]
    assert call("POST", "/api/reboot")[0] == 403
    assert len(iface.sent) == before  # nothing reached the radio
    assert call("POST", "/api/tx", {"enabled": True})[0] == 200
    assert call("POST", "/api/send", {"text": "hello", "to": BOB})[0] == 200
    log = call("GET", "/api/tx")[1]["log"]
    assert [(r["kind"], r["allowed"]) for r in log[:3]] == [("dm", 1), ("config", 0), ("dm", 0)]


# ---- tokens for other apps ------------------------------------------------


def test_read_token_can_read_but_not_send(api, store):
    call, iface, _ = api
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 1, "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3,
                         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"}})
    token = store.create_token("display", {"read"})
    status, body = call("GET", "/api/messages?since=1h", token=token)
    assert status == 200 and body["messages"][0]["text"] == "hello"
    nodes = call("GET", "/api/nodes", token=token)[1]["nodes"]
    assert [(n["num"], n["via"], n["last_snr"]) for n in nodes] == [(BOB, "radio", 5.0)]
    packet = call("GET", f"/api/packets?node=!{BOB:08x}&type=TEXT_MESSAGE_APP", token=token)[1]["packets"][0]
    assert packet["packet"]["decoded"]["text"] == "hello"  # the full packet, parsed
    before = len(iface.sent)
    assert call("POST", "/api/send", {"text": "hi", "to": BOB}, token=token)[0] == 403
    assert len(iface.sent) == before
    assert call("GET", "/api/status", token="mst_not-a-real-token")[0] == 401
    store.revoke_token("display")
    assert call("GET", "/api/nodes", token=token)[0] == 401


def test_send_token_is_budgeted_and_kept_off_config(api, store):
    call, iface, _ = api
    token = store.create_token("pager", {"read", "send"})
    status, body = call("POST", "/api/send", {"text": "one", "to": BOB}, token=token)
    assert status == 200
    status, body = call("POST", "/api/send", {"text": "two", "to": BOB}, token=token)
    assert status == 429 and "30 s apart" in body["error"]  # unattended sends are spaced
    status, body = call("POST", "/api/send", {"text": "to everyone", "channel": 0}, token=token)
    assert status == 403 and "Broadcasts aren't allowed" in body["error"]
    for path, body in (("/api/reboot", {}), ("/api/tx", {"enabled": False}),
                       ("/api/config/role", {"role": "CLIENT"})):
        status, result = call("POST", path, body, token=token)
        assert status == 403 and "only the MeshShack app" in result["error"]
    log = store.tx_log()
    assert log[-1]["source"] == "api:pager" and log[-1]["allowed"] == 1


def test_event_stream_delivers_packets(store, tmp_path):
    from meshshack.events import EventBus, packet_event

    bus = EventBus()
    collector = SimpleNamespace(iface=FakeInterface(), connected_port="/dev/ttyACM0")
    server = ApiServer(Radio(collector, store), tmp_path / "hub.json", port=0, bus=bus)
    server.start()
    try:
        token = store.create_token("listener", {"read"})
        req = urllib.request.Request(server.url + "/api/events", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.readline() == b": connected\n"
            resp.readline()
            packet = {"from": BOB, "to": BROADCAST_NUM, "id": 7, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
            for event in packet_event(packet, 1, False):
                bus.publish(event)
            lines = [resp.readline() for _ in range(6)]
        assert lines[0] == b"event: packet\n" and lines[3] == b"event: message\n"
        assert json.loads(lines[4][len(b"data: "):])["text"] == "hi"
    finally:
        server.stop()



# ---- channels -------------------------------------------------------------


def test_add_share_and_protect_channels(api, store):
    import base64
    from meshtastic.protobuf import apponly_pb2

    call, iface, _ = api
    status, body = call("POST", "/api/channels/add", {"name": "Private", "position_precision": 13})
    assert status == 200 and body == {"index": 2}
    new = iface.localNode.channels[2]
    assert new.role == channel_pb2.Channel.Role.SECONDARY and len(new.settings.psk) == 32  # random AES-256
    assert ("writeChannel", (2,), {}) in iface.localNode.calls

    channels = {c["name"]: c for c in call("GET", "/api/channels")[1]["channels"]}
    assert channels["Private"]["encryption"] == "private-256" and channels[""]["encryption"] == "none"
    url = channels["Private"]["share_url"]
    assert url.startswith("https://meshtastic.org/e/?add=true#")
    shared = apponly_pb2.ChannelSet()
    shared.ParseFromString(base64.urlsafe_b64decode(url.split("#")[1] + "=" * (-len(url.split("#")[1]) % 4)))
    assert [c.name for c in shared.settings] == ["Private"]  # only that channel is shared
    assert shared.lora_config.region == config_pb2.Config.LoRaConfig.RegionCode.US

    assert call("POST", "/api/channels/add", {"name": "Private"})[0] == 400  # duplicate name
    assert call("POST", "/api/channels/add", {"name": "much-too-long-name"})[0] == 400
    assert call("POST", "/api/channels/add", {"name": "Bad", "key": "c2hvcnQ="})[0] == 400  # 5-byte key
    status, body = call("POST", "/api/channels/update", {"index": 0, "name": "Mine"})
    assert status == 403 and "public mesh" in body["error"]
    assert call("POST", "/api/channels/update", {"index": 0, "position_precision": 16})[0] == 200
    assert iface.localNode.channels[0].settings.module_settings.position_precision == 16
    assert call("POST", "/api/channels/delete", {"index": 0})[0] == 403
    assert call("POST", "/api/channels/delete", {"index": 1})[0] == 200

    token = store.create_token("reader", {"read"})
    assert call("GET", "/api/channels", token=token)[0] == 403  # keys are for the app only
    call("POST", "/api/tx", {"enabled": False})
    status, body = call("POST", "/api/channels/update", {"index": 0, "position_precision": 13})
    assert status == 403 and "turned off" in body["error"]  # the kill switch covers config too


def test_favorite_and_ignore_are_local_settings(api, store):
    call, iface, _ = api
    call("POST", "/api/tx", {"enabled": False})  # nothing goes on the air, so the kill switch doesn't apply
    assert call("POST", "/api/nodes/favorite", {"node": f"!{BOB:08x}", "favorite": True})[0] == 200
    assert ("setFavorite", (BOB,), {}) in iface.localNode.calls
    assert store.node(BOB)["is_favorite"] == 1
    assert call("POST", "/api/nodes/ignore", {"node": BOB, "ignored": True})[0] == 200
    assert call("POST", "/api/nodes/ignore", {"node": BOB, "ignored": False})[0] == 200
    assert ("removeIgnored", (BOB,), {}) in iface.localNode.calls and store.node(BOB)["is_ignored"] == 0
    assert call("POST", "/api/nodes/favorite", {"node": ME, "favorite": True})[0] == 400  # that's us
    row = store.tx_log()[0]
    assert (row["kind"], row["cost"], row["allowed"]) == ("setting", 0, 1)
    token = store.create_token("tool", {"read", "send"})
    assert call("POST", "/api/nodes/favorite", {"node": BOB, "favorite": False}, token=token)[0] == 403


def test_radio_snapshot_carries_favorites(store):
    store.record_node_info({"num": BOB, "isFavorite": True})
    assert store.node(BOB)["is_favorite"] == 1
    store.record_node_info({"num": BOB})  # the radio leaves false flags out
    assert store.node(BOB)["is_favorite"] == 0



def test_replies_and_reactions(api, store):
    call, iface, _ = api
    status, body = call("POST", "/api/send", {"text": "agreed", "to": BOB, "reply_id": 4242})
    assert status == 200 and iface.sent[-1][2]["replyId"] == 4242

    status, body = call("POST", "/api/send", {"text": "👍", "to": BOB, "reply_id": 4242, "emoji": True})
    assert status == 200
    packet, dest, kw = iface.sent[-1]
    assert (packet.decoded.emoji, packet.decoded.reply_id, packet.decoded.payload.decode()) == (1, 4242, "👍")
    assert dest == BOB and kw["wantAck"]
    [reaction] = [m for m in store.thread(peer=BOB) if m["emoji"]]
    assert (reaction["reply_id"], reaction["text"], reaction["direction"]) == (4242, "👍", "out")

    assert call("POST", "/api/send", {"text": "👍", "to": BOB, "emoji": True})[0] == 400  # reacting to what?
    assert call("POST", "/api/send", {"text": "ok", "to": BOB, "reply_id": 4242, "emoji": True})[0] == 400
    assert call("POST", "/api/send", {"text": "hi", "to": BOB, "reply_id": "abc"})[0] == 400
    assert store.tx_log()[0]["kind"] == "dm"  # reactions go through the gatekeeper like any message


def test_coverage_endpoint(api, store):
    call, iface, _ = api
    store.record_packet({"from": BOB, "to": BROADCAST_NUM, "id": 1, "rxSnr": -6.0, "hopStart": 3, "hopLimit": 3,
                         "decoded": {"portnum": "POSITION_APP"}})
    token = store.create_token("mapper", {"read"})
    status, body = call("GET", "/api/coverage?since=1h", token=token)
    assert status == 200 and body["neighbors"][0]["num"] == BOB and body["totals"]["direct"] == 1
