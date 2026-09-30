"""Holds the MeshCore radio's USB connection and logs what it hears, alongside the Meshtastic one.

Receive only: nothing here can make the radio transmit. Everything it asks the radio (its identity,
contact list, channel names, queued messages) stays on the USB cable. It uses only the radio linked
as MeshCore (see devices.py), found by hardware ID wherever it's plugged in.

It uses the meshcore library (MIT), connecting with DTR and RTS off: on boards whose USB is the
ESP32's own (the Heltec V4), those lines are wired to reset and boot mode, and the library's usual
settings would leave the radio in its firmware loader. Opening the port still restarts such a board,
so the first "app start" can go unanswered while it boots; it's asked again.
"""

import asyncio
import logging
import time

from . import devices

log = logging.getLogger("meshshack")

TYPE_NAMES = {1: "chat", 2: "repeater", 3: "room server", 4: "sensor"}
CONTACTS_EVERY = 300  # seconds between contact-list refreshes (local to the radio)


class MeshCoreCollector:
    def __init__(self, store, retry_seconds=15, bus=None):
        self.store = store
        self.bus = bus
        self.retry_seconds = retry_seconds
        self.connected_port = None
        self.connected_hwid = None
        self.info = None  # the radio's identity and settings while connected
        self.channels = {}  # slot -> name
        self._last_warning = None

    # ---- lifecycle ----

    def run(self, stop):
        """Connect, log, reconnect, until `stop` (a threading.Event) is set. Blocks; run it in a thread."""
        try:
            import meshcore  # noqa: F401
        except ImportError:
            log.info("MeshCore support needs the meshcore package (pip install meshcore); skipping it")
            return
        asyncio.run(self._main(stop))

    async def _main(self, stop):
        while not stop.is_set():
            port = self._find_port()
            if port:
                try:
                    await self._session(port, stop)
                except Exception as ex:
                    log.error("MeshCore radio on %s: %s", port, ex)
                    self.store.record_event("error", f"meshcore {port}: {ex}")
            await self._sleep(stop, self.retry_seconds)

    @staticmethod
    async def _sleep(stop, seconds):
        end = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.5)

    def _warn(self, message, level=logging.WARNING):
        if message != self._last_warning:
            log.log(level, message)
            self._last_warning = message

    def _find_port(self):
        hwid = devices.linked(self.store, "meshcore")
        if not hwid:
            self._warn("No MeshCore radio linked; MeshCore logging is off", logging.INFO)
            return None
        found = devices.find(hwid)
        if not found:
            self._warn(f"The linked MeshCore radio ({hwid}) isn't plugged in; waiting for it")
            return None
        self._last_warning = None
        return found["port"]

    async def _session(self, port, stop):
        from meshcore import EventType, MeshCore
        from meshcore.serial_cx import SerialConnection

        mc = MeshCore(SerialConnection(port, 115200, rts=False, dtr=False))
        lost = asyncio.Event()
        try:
            log.info("MeshCore: connecting to %s", port)
            started = await mc.connect()
            for _ in range(4):  # the board restarts when its port opens; ask again once it's up
                if started is not None and started.type != EventType.ERROR:
                    break
                await asyncio.sleep(1.5)
                started = await mc.commands.send_appstart(timeout=2)
            else:
                if started is None or started.type == EventType.ERROR:
                    raise ConnectionError("no answer from a MeshCore companion radio")

            await self._connected(mc, port)
            mc.subscribe(EventType.DISCONNECTED, lambda e: lost.set())
            mc.subscribe(EventType.RX_LOG_DATA, self._on_packet)
            mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_message)
            mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
            await mc.start_auto_message_fetching()
            await mc.commands.get_msg()  # anything that queued up while nobody was connected

            wanted = self.connected_hwid
            next_contacts = time.monotonic() + CONTACTS_EVERY
            while not stop.is_set() and not lost.is_set():
                await asyncio.sleep(1)
                if devices.linked(self.store, "meshcore") != wanted:
                    log.info("MeshCore: the linked radio changed; letting go of %s", port)
                    break
                if time.monotonic() >= next_contacts:
                    await self._sync_contacts(mc)
                    next_contacts = time.monotonic() + CONTACTS_EVERY
            if lost.is_set():
                log.warning("MeshCore: connection lost")
                self.store.record_event("disconnected", "meshcore: connection lost")
        finally:
            was_connected = self.connected_port is not None
            self.connected_port = self.connected_hwid = self.info = None
            if was_connected:
                self._publish({"type": "meshcore", "kind": "connection", "connected": False})
            try:
                await mc.disconnect()
            except Exception as ex:
                log.debug("MeshCore: error closing: %s", ex)

    async def _connected(self, mc, port):
        me = dict(mc.self_info or {})
        device = await mc.commands.send_device_query()
        dev = device.payload if device and isinstance(device.payload, dict) else {}
        self.info = {
            "name": me.get("name"),
            "public_key": me.get("public_key"),
            "model": dev.get("model"),
            "firmware": dev.get("ver"),
            "freq_mhz": me.get("radio_freq"),
            "bw_khz": me.get("radio_bw"),
            "sf": me.get("radio_sf"),
            "cr": me.get("radio_cr"),
            "tx_power": me.get("tx_power"),
        }
        self.channels = {}
        for idx in range(min(int(dev.get("max_channels") or 8), 40)):
            reply = await mc.commands.get_channel(idx)  # also hands the key to the library, for decoding
            name = (reply.payload or {}).get("channel_name") if reply else None
            if name:
                self.channels[idx] = name
        mc.set_decrypt_channel_logs(True)  # so heard channel packets can be matched to messages
        self.store.set_station("meshcore_radio", {**self.info, "channels": self.channels})
        await self._sync_contacts(mc)
        self.connected_port = port
        self.connected_hwid = (devices.find_port(port) or {}).get("hardware_id")
        detail = f"{port} as {self.info['name']} ({self.info['model']}, {self.info['firmware']}, " \
                 f"{self.info['freq_mhz']} MHz)"
        log.info("MeshCore: connected: %s; channels: %s", detail, ", ".join(self.channels.values()) or "none")
        self.store.record_event("connected", f"meshcore {detail}")
        self._publish({"type": "meshcore", "kind": "connection", "connected": True, "port": port})

    async def _sync_contacts(self, mc):
        reply = await mc.commands.get_contacts()
        contacts = reply.payload if reply and isinstance(reply.payload, dict) else {}
        for c in contacts.values():
            try:
                self.store.mc_record_contact(c)
            except Exception:
                log.exception("MeshCore: failed to record contact")

    # ---- events (on the collector's event loop) ----

    def _on_packet(self, event):
        d = event.payload or {}
        try:
            self.store.mc_record_packet(d)
        except Exception:
            log.exception("MeshCore: failed to record packet")
            return
        hops = self.store.mc_hops(d.get("route_typename"), d.get("path_len"))
        line = f"MC {d.get('payload_typename', '?'):<9} snr={d.get('snr')} rssi={d.get('rssi')}"
        if hops is not None:
            line += f" hops={hops}"
        if d.get("adv_name"):
            line += f" advert from {d['adv_name']} ({TYPE_NAMES.get(d.get('adv_type'), '?')})"
        log.info(line)
        self._publish({"type": "meshcore", "kind": "packet", "payload_type": d.get("payload_typename")})

    def _on_message(self, event):
        m = event.payload or {}
        try:
            channel = self.channels.get(m.get("channel_idx")) if m.get("type") == "CHAN" else None
            self.store.mc_record_message(m, channel_name=channel)
        except Exception:
            log.exception("MeshCore: failed to record message")
            return
        where = f"#{channel}" if m.get("type") == "CHAN" else f"DM from {m.get('pubkey_prefix')}"
        log.info("MC message %s: %s", where, m.get("text"))
        self._publish({"type": "meshcore", "kind": "message"})

    def _publish(self, event):
        if self.bus is not None:
            self.bus.publish(event)

    def status(self):
        if self.connected_port is None:
            return {"connected": False, "linked": devices.linked(self.store, "meshcore")}
        return {"connected": True, "port": self.connected_port, "hardware_id": self.connected_hwid,
                "radio": self.info, "channels": [{"index": i, "name": n} for i, n in sorted(self.channels.items())]}
