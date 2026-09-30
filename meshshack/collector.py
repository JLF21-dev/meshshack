"""Holds the USB connection to the radio and feeds everything it hears into the Store.

Which radio: the one given with --port, else the one linked as this station's Meshtastic radio
(found by hardware ID wherever it's plugged in; see devices.py), else the only unlinked
Meshtastic-looking device, which then gets linked so it's found again next time.
"""

import logging
import os

import meshtastic.serial_interface
import meshtastic.util
from pubsub import pub

from . import alerts, devices
from .events import packet_event
from .store import hops_taken

log = logging.getLogger("meshshack")


class Collector:
    def __init__(self, store, port=None, retry_seconds=15, bus=None):
        self.store = store
        self.bus = bus  # optional EventBus for the API's live stream
        self.port = port
        self.retry_seconds = retry_seconds
        self._last_warning = None  # log each "can't pick a radio" situation once, not every retry
        # The live interface while connected, for the API to send through; None otherwise.
        self.iface = None
        self.connected_port = None
        self.connected_hwid = None
        self._switching = False  # the link changed: reconnect now instead of after the retry wait

    def run(self, stop):
        """Connect, log until the connection drops, reconnect. Returns once `stop` is set."""
        # pubsub holds weak references to listeners; these bound methods live as long as self.
        # Subscribing to the parent topic delivers every meshtastic.receive.* subtopic too.
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_node_updated, "meshtastic.node.updated")
        try:
            while not stop.is_set():
                iface = self._connect()
                if iface is not None:
                    self.iface = iface
                    self._publish({"type": "connection", "connected": True, "port": self.connected_port})
                    self._watch(iface, stop)
                    self.iface = self.connected_port = self.connected_hwid = None
                    self._publish({"type": "connection", "connected": False})
                    self._close(iface)
                if self._switching:
                    self._switching = False
                elif not stop.is_set():
                    stop.wait(self.retry_seconds)
        finally:
            pub.unsubscribe(self.on_receive, "meshtastic.receive")
            pub.unsubscribe(self.on_node_updated, "meshtastic.node.updated")

    def _warn(self, message):
        if message != self._last_warning:
            log.warning(message)
            self._last_warning = message

    def _find_port(self):
        if self.port:
            return self.port
        ports = devices.scan()
        wanted = devices.linked(self.store, "meshtastic")
        if wanted:
            found = devices.find(wanted, ports)
            if found:
                self._last_warning = None
                return found["port"]
            self._warn(f"The linked Meshtastic radio ({wanted}) isn't plugged in; waiting for it "
                       f"(checking every {self.retry_seconds}s)")
            return None
        # Nothing linked yet: only a device that looks like a Meshtastic radio and isn't linked as
        # something else (a MeshCore radio is an ESP32 too).
        taken = set(devices.links(self.store))
        likely = set(meshtastic.util.findPorts(True))
        candidates = [p for p in ports if p["port"] in likely and p["hardware_id"] not in taken]
        if len(candidates) == 1:
            self._last_warning = None
            return candidates[0]["port"]
        if candidates:
            self._warn(f"Several possible radios ({', '.join(p['port'] for p in candidates)}): choose one with "
                       "Scan in the Device tab, `meshshack radios link`, or --port")
        else:
            self._warn(f"No Meshtastic device found; will keep checking every {self.retry_seconds}s")
        return None

    def _hardware_id(self, port):
        real = os.path.realpath(port)
        for p in devices.scan():
            if os.path.realpath(p["port"]) == real:
                return p["hardware_id"]
        return None

    def _connect(self):
        port = self._find_port()
        if port is None:
            return None
        log.info("Connecting to %s", port)
        try:
            # SerialInterface blocks until the radio has sent its config and node database.
            # The library calls sys.exit() on some errors, hence SystemExit.
            iface = meshtastic.serial_interface.SerialInterface(devPath=port)
        except (Exception, SystemExit) as ex:
            log.error("Could not connect to %s: %s", port, ex)
            self.store.record_event("error", f"connect {port}: {ex}")
            return None

        me = iface.getMyNodeInfo() or {}
        user = me.get("user") or {}
        detail = f"{port} as {user.get('id', '?')} {user.get('longName', '')}".strip()
        log.info("Connected: %s; %d nodes in radio's database", detail, len(iface.nodesByNum or {}))
        self.store.record_event("connected", detail)
        my_info = getattr(iface, "myInfo", None)
        if my_info is not None:
            self.store.mark_local(my_info.my_node_num)
        for node in list((iface.nodesByNum or {}).values()):
            self.store.record_node_info(node)
        self.connected_port = port
        self.connected_hwid = self._hardware_id(port)
        linked = devices.linked(self.store, "meshtastic")
        if self.connected_hwid and not linked:
            devices.link(self.store, self.connected_hwid, "meshtastic", label=user.get("longName"), node=user.get("id"))
            log.info("Linked this radio (hardware ID %s) as the station's Meshtastic radio; it will be found "
                     "by that ID from now on, whichever USB port it's in", self.connected_hwid)
        elif linked == self.connected_hwid:
            devices.describe(self.store, linked, label=user.get("longName"), node=user.get("id"))
        return iface

    def _link_changed(self):
        """True when a different radio has been linked than the one we're connected to."""
        if self.port:
            return False
        wanted = devices.linked(self.store, "meshtastic")
        return wanted is not None and wanted != self.connected_hwid

    def _watch(self, iface, stop):
        # The library clears isConnected when the reader thread dies (unplugged) or the
        # radio reboots. Either way we tear down and reconnect from scratch.
        ticks = 0
        while not stop.is_set() and iface.isConnected.is_set():
            stop.wait(1)
            ticks += 1
            if ticks % 5 == 0 and self._link_changed():
                log.info("A different radio was linked; switching to it")
                self.store.record_event("disconnected", "switching to the newly linked radio")
                self._switching = True
                return
        if not stop.is_set():
            log.warning("Connection lost")
            self.store.record_event("disconnected", "connection lost")

    def _close(self, iface):
        try:
            iface.close()
        except Exception as ex:
            log.debug("Error closing interface: %s", ex)

    # ---- pubsub listeners (run on the library's publishing thread) ----

    def on_receive(self, packet, interface):
        try:
            my_info = getattr(interface, "myInfo", None)
            local = my_info is not None and packet.get("from") == my_info.my_node_num
            row = self.store.record_packet(packet, local=local)
            log.info(self.describe(packet))
            for event in packet_event(packet, row, local):
                self._publish(event)
            alert = alerts.check(self.store, packet, row, local)
            if alert is not None:
                log.warning("ALERT (%s) from %s: %s", alert["reason"], self.store.node_label(alert["from_num"]),
                            alert["text"])
                self._publish({"type": "alert", **alert})
        except Exception:
            log.exception("Failed to record packet")

    def _publish(self, event):
        if self.bus is not None:
            self.bus.publish(event)

    def on_node_updated(self, node, interface):
        try:
            self.store.record_node_info(node)
        except Exception:
            log.exception("Failed to record node update")

    def describe(self, packet):
        decoded = packet.get("decoded") or {}
        portnum = decoded.get("portnum") or ("ENCRYPTED" if "encrypted" in packet else "UNKNOWN")
        parts = [
            f"{self.store.node_label(packet.get('from'))} -> {self.store.node_label(packet.get('to'))}",
            f"ch{packet.get('channel', 0)}",
        ]
        if packet.get("rxSnr") is not None:
            parts.append(f"snr={packet['rxSnr']}")
        if packet.get("rxRssi") is not None:
            parts.append(f"rssi={packet['rxRssi']}")
        hops = hops_taken(packet)
        if hops is not None:
            parts.append(f"hops={hops}")
        if packet.get("viaMqtt"):
            parts.append("mqtt")
        line = f"{portnum:<18} " + " ".join(parts)
        if portnum == "TEXT_MESSAGE_APP" and decoded.get("text") is not None:
            line += f": {decoded['text']}"
        return line
