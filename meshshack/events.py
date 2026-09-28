"""In-process event bus: the collector publishes what it logs; API streams subscribe.

Each subscriber gets its own bounded queue. A slow client loses its oldest events rather
than slowing down logging, and is told so with a "dropped" count on the next event.
"""

import queue
import threading

QUEUE_SIZE = 500


class Subscription:
    def __init__(self):
        self.queue = queue.Queue(QUEUE_SIZE)
        self.dropped = 0

    def get(self, timeout):
        """The next event, or None after `timeout` seconds of quiet."""
        try:
            event = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if self.dropped:
            event = {**event, "dropped": self.dropped}
            self.dropped = 0
        return event


class EventBus:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self):
        sub = Subscription()
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            self._subs.discard(sub)

    def count(self):
        with self._lock:
            return len(self._subs)

    def publish(self, event):
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            while True:
                try:
                    sub.queue.put_nowait(event)
                    break
                except queue.Full:
                    try:
                        sub.queue.get_nowait()
                        sub.dropped += 1
                    except queue.Empty:
                        pass


def packet_event(packet, row, local):
    """What a stream client sees for each logged packet (and a "message" for text)."""
    from .store import hops_taken, to_jsonable

    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum") or ("ENCRYPTED" if "encrypted" in packet else "UNKNOWN")
    event = {
        "type": "packet", "row": row, "id": packet.get("id"), "from": packet.get("from"), "to": packet.get("to"),
        "channel": packet.get("channel", 0), "portnum": portnum, "rx_snr": packet.get("rxSnr"),
        "rx_rssi": packet.get("rxRssi"), "hops": hops_taken(packet), "via_mqtt": bool(packet.get("viaMqtt")),
        "local": local, "decoded": to_jsonable(decoded),
    }
    if portnum == "TEXT_MESSAGE_APP" and decoded.get("text") is not None:
        return [event, {"type": "message", "row": row, "from": event["from"], "to": event["to"],
                        "channel": event["channel"], "text": decoded["text"], "via_mqtt": event["via_mqtt"],
                        "hops": event["hops"], "reply_id": decoded.get("replyId"), "emoji": decoded.get("emoji")}]
    return [event]
