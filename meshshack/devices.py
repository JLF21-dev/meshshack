"""Finding radios on USB and remembering which is which.

Each radio is known by its hardware ID: the chip's MAC address, which ESP32 boards report as the
USB serial number (Meshtastic firmware as "240AC4000001", the chip's own USB as "24:0A:C4:00:00:01").
It stays the same whichever USB socket the radio is in and whatever firmware it runs, so a
linked radio is found again after it's moved; /dev/ttyACM0 and friends are just where it is today.

Links live in the station table under "radios": {hardware ID: {"kind", "label", "node", "linked_at"}}.
The logger uses the radio linked as Meshtastic, and never grabs one that's linked as something else.

A probe asks a radio what it is, and only things that stay on the USB cable: MeshCore's
"app start" (answered with its identity) and Meshtastic's "send me your config" (answered with
its node number and firmware). Neither makes the radio transmit. Ports another program has
open are never probed, and every port is opened with DTR and RTS off: on ESP32 boards whose USB
is the chip's own, those lines are wired to reset and boot mode, and the usual defaults would
drop the radio into its firmware loader. Linux still pulses them for a moment on open, which
restarts such a board (MeshCore on a Heltec V4 does this); the probe waits for it to come back.
"""

import os
import time

KINDS = {"meshtastic": "Meshtastic", "meshcore": "MeshCore"}
STATION_KEY = "radios"


def hardware_id(serial_number):
    """'24:0A:C4:00:00:02' and '240AC4000002' -> '240AC4000002'. None when there's nothing to go by."""
    if not serial_number:
        return None
    return "".join(ch for ch in serial_number if ch.isalnum()).upper() or None


def _by_id_paths():
    """{/dev/ttyACM0: /dev/serial/by-id/...}: the stable name udev gives each port, when there is one."""
    out = {}
    base = "/dev/serial/by-id"
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            out[os.path.realpath(path)] = path
    except OSError:
        pass
    return out


def ports_in_use():
    """Real paths of the serial ports some process (of ours) has open, from /proc on Linux."""
    used = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return used
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith("/dev/tty"):
                    used.add(target)
        except OSError:
            continue
    return used


def scan(list_ports=None):
    """USB serial ports right now: [{port, stable_path, hardware_id, vid, pid, description, location}]."""
    if list_ports is None:
        from serial.tools import list_ports as lp

        list_ports = lp.comports
    by_id = _by_id_paths()
    out = []
    for p in list_ports():
        if p.vid is None:  # built-in serial ports (ttyS*), not a radio
            continue
        out.append({
            "port": p.device,
            "stable_path": by_id.get(os.path.realpath(p.device)),
            "hardware_id": hardware_id(p.serial_number),
            "vid": p.vid,
            "pid": p.pid,
            "description": p.description if p.description != "n/a" else p.product,
            "location": p.location,
        })
    return sorted(out, key=lambda d: d["port"])


# ---- probing ----

def _open(port, timeout=0.2):
    import serial

    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, timeout
    s.dtr = s.rts = False  # set before opening, so the lines never pulse
    s.open()
    return s


def _read_for(s, seconds, done=None):
    buf, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        buf += s.read(4096)
        if done and done(buf):
            break
    return buf


def meshcore_frames(buf):
    """Companion-protocol replies: '>' + little-endian length + payload."""
    out = []
    while True:
        i = buf.find(b">")
        if i < 0 or len(buf) < i + 3:
            return out
        n = int.from_bytes(buf[i + 1:i + 3], "little")
        if len(buf) < i + 3 + n:
            return out
        out.append(buf[i + 3:i + 3 + n])
        buf = buf[i + 3 + n:]


def meshcore_self_info(payload):
    """The SELF_INFO reply (code 5) to app start: identity and radio settings."""
    if len(payload) < 58 or payload[0] != 5:
        return None
    return {
        "name": payload[58:].split(b"\0")[0].decode(errors="replace"),
        "public_key": payload[4:36].hex(),
        "tx_power": payload[2],
        "freq_mhz": int.from_bytes(payload[48:52], "little") / 1000,
        "bw_khz": int.from_bytes(payload[52:56], "little") / 1000,
        "sf": payload[56],
        "cr": payload[57],
    }


def _probe_meshcore(s, attempts=1):
    """Ask again while a freshly restarted radio is still starting up."""
    app_start = bytes([1, 3]) + b"mshack"  # CMD_APP_START, protocol version 3, our name
    for _ in range(attempts):
        s.reset_input_buffer()
        s.write(b"<" + len(app_start).to_bytes(2, "little") + app_start)
        buf = _read_for(s, 1.5, done=lambda b: any(f[:1] == b"\x05" for f in meshcore_frames(b)))
        for frame in meshcore_frames(buf):
            info = meshcore_self_info(frame)
            if info:
                return {"kind": "meshcore", "node": info["name"], "detail": info}
    return None


def meshtastic_frames(buf):
    """Meshtastic serial framing: 0x94 0xC3 + big-endian length + a FromRadio protobuf."""
    out = []
    while True:
        i = buf.find(b"\x94\xc3")
        if i < 0 or len(buf) < i + 4:
            return out
        n = int.from_bytes(buf[i + 2:i + 4], "big")
        if n > 512 or len(buf) < i + 4 + n:
            if n > 512:  # not a real header; keep looking past it
                buf = buf[i + 2:]
                continue
            return out
        out.append(buf[i + 4:i + 4 + n])
        buf = buf[i + 4 + n:]


def _probe_meshtastic(s):
    from meshtastic.protobuf import mesh_pb2

    def send(message):
        raw = message.SerializeToString()
        s.write(b"\x94\xc3" + len(raw).to_bytes(2, "big") + raw)

    s.dtr = True  # Meshtastic's USB serial only talks to a host that has raised DTR
    s.reset_input_buffer()
    found = {}

    def complete(buf):  # re-reads what has arrived; it stops at the first few frames it needs
        for raw in meshtastic_frames(buf):
            msg = mesh_pb2.FromRadio()
            try:
                msg.ParseFromString(raw)
            except Exception:
                continue
            kind = msg.WhichOneof("payload_variant")
            if kind == "my_info":
                found["num"] = msg.my_info.my_node_num
            elif kind == "metadata":
                found["firmware"] = msg.metadata.firmware_version
            elif kind == "node_info" and msg.node_info.num == found.get("num"):
                found["name"] = msg.node_info.user.long_name
            elif kind == "config_complete_id":
                return True
        return "num" in found and "firmware" in found and "name" in found

    try:
        # A radio that another client just left can take a few seconds to answer; ask again.
        buf = b""
        for _ in range(3):
            s.write(b"\xc3" * 32)  # wake the serial API, as the Meshtastic library does
            time.sleep(0.1)
            send(mesh_pb2.ToRadio(want_config_id=0x4D534B))
            buf += _read_for(s, 2.5, done=lambda b: complete(buf + b))
            if "num" in found:
                _read_for(s, 1.5, done=lambda b: complete(buf + b))
                break
    finally:
        # Say goodbye, as the library does on close. Without it the firmware keeps trying to stream
        # its config to a client that has gone, and can stop answering on USB until it's reset.
        send(mesh_pb2.ToRadio(disconnect=True))
        s.flush()
        time.sleep(0.2)
    if "num" not in found:
        return None
    return {"kind": "meshtastic", "node": f"!{found['num']:08x}",
            "detail": {"name": found.get("name"), "firmware": found.get("firmware")}}


def probe(port):
    """What firmware answers on this port: {"kind", "node", "detail"}, or {"kind": None, "error"}."""
    try:
        s = _open(port)
    except Exception as ex:
        return {"kind": None, "error": f"couldn't open: {ex}"}
    try:
        # Opening can reset boards whose USB is the chip's own (the kernel raises DTR/RTS before
        # anyone can clear them). If the boot ROM starts talking, give the firmware time to start.
        boot_output = _read_for(s, 1.2)
        restarted = any(mark in boot_output for mark in (b"ESP-ROM", b"rst:", b"entry 0x"))
        return (_probe_meshcore(s, attempts=4 if restarted else 1) or _probe_meshtastic(s)
                or {"kind": None, "error": "no answer (a Meshtastic radio that was just in use may need another scan)"})
    except Exception as ex:
        return {"kind": None, "error": str(ex)}
    finally:
        s.close()


# ---- links ----

def links(store):
    return store.station(STATION_KEY) or {}


def linked(store, kind):
    """The hardware ID linked as this kind, or None."""
    for hwid, link in links(store).items():
        if link.get("kind") == kind:
            return hwid
    return None


def link(store, hwid, kind, label=None, node=None, now=None):
    """Link a radio. One radio per kind: linking another one of the same kind replaces it."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    hwid = hardware_id(hwid)
    if not hwid:
        raise ValueError("no hardware ID")
    current = {h: l for h, l in links(store).items() if l.get("kind") != kind and h != hwid}
    current[hwid] = {"kind": kind, "label": label, "node": node, "linked_at": now or time.time()}
    store.set_station(STATION_KEY, current)
    store.record_event("radio", f"linked {hwid} as {KINDS[kind]}" + (f" ({node})" if node else ""))
    return current[hwid]


def describe(store, hwid, label=None, node=None):
    """Keep a link's node ID and name current (e.g. once the logger has talked to the radio)."""
    current = links(store)
    entry = current.get(hardware_id(hwid))
    if entry is None or (entry.get("label"), entry.get("node")) == (label, node):
        return
    entry.update(label=label, node=node)
    store.set_station(STATION_KEY, current)


def unlink(store, hwid):
    hwid = hardware_id(hwid)
    current = links(store)
    if current.pop(hwid, None) is None:
        return False
    store.set_station(STATION_KEY, current or None)
    store.record_event("radio", f"unlinked {hwid}")
    return True


def find(hwid, ports=None):
    """Where this radio is plugged in right now: its port entry, or None."""
    for p in scan() if ports is None else ports:
        if p["hardware_id"] == hwid:
            return p
    return None


def find_port(port, ports=None):
    """The scan entry for a port given by any of its names (/dev/ttyACM0 or its by-id link), or None."""
    real = os.path.realpath(port)
    for p in scan() if ports is None else ports:
        if os.path.realpath(p["port"]) == real:
            return p
    return None


def survey(store, probe_ports=False, skip=(), ports=None, prober=None):
    """Every radio: what's plugged in now (probed on request) plus linked radios that aren't.
    `skip` are ports not to probe (e.g. the one the logger holds); busy ports are skipped too."""
    ports = scan() if ports is None else ports
    known = links(store)
    busy = ports_in_use() | {os.path.realpath(p) for p in skip if p}
    out = []
    for p in ports:
        row = {**p, "link": known.get(p["hardware_id"]), "present": True, "in_use": os.path.realpath(p["port"]) in busy}
        if probe_ports and not row["in_use"]:
            row["probe"] = (prober or probe)(p["port"])
        out.append(row)
    present = {p["hardware_id"] for p in ports}
    for hwid, l in known.items():
        if hwid not in present:
            out.append({"port": None, "stable_path": None, "hardware_id": hwid, "description": None,
                        "link": l, "present": False, "in_use": False})
    return out
