"""Holds the USB connection to the radio and feeds everything it hears into the Store."""

import logging

import meshtastic.serial_interface
import meshtastic.util
from pubsub import pub

from .events import packet_event
from .store import hops_taken

log = logging.getLogger("meshshack")


class Collector:
    def __init__(self, store, port=None, retry_seconds=15, bus=None):
        self.store = store
        self.bus = bus  # optional EventBus for the API's live stream
        self.port = port
        self.retry_seconds = retry_seconds
        self._warned_no_port = False
        # The live interface while connected, for the API to send through; None otherwise.
        self.iface = None
        self.connected_port = None

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
                    self.iface = self.connected_port = None
                    self._publish({"type": "connection", "connected": False})
                    self._close(iface)
                if not stop.is_set():
                    stop.wait(self.retry_seconds)
        finally:
            pub.unsubscribe(self.on_receive, "meshtastic.receive")
            pub.unsubscribe(self.on_node_updated, "meshtastic.node.updated")

    def _find_port(self):
        if self.port:
            return self.port
        ports = meshtastic.util.findPorts(True)
        if len(ports) == 1:
            self._warned_no_port = False
            return ports[0]
        if len(ports) > 1:
            log.error("Multiple serial devices found (%s); choose one with --port", ", ".join(ports))
        elif not self._warned_no_port:
            log.warning("No Meshtastic device found; will keep checking every %ss", self.retry_seconds)
            self._warned_no_port = True
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
        return iface

    def _watch(self, iface, stop):
        # The library clears isConnected when the reader thread dies (unplugged) or the
        # radio reboots. Either way we tear down and reconnect from scratch.
        while not stop.is_set() and iface.isConnected.is_set():
            stop.wait(1)
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
