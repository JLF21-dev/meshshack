"""Emergency detection: flag possible emergencies in what the radio hears. Nothing is transmitted.

Runs in the logger (so alerts are caught while the app is closed) on every received packet:
- ALERT_APP: Meshtastic's port for critical alerts ("same as a text message, but for alerts").
- The alert bell: a text message containing the BEL character, which the Meshtastic apps'
  alert button sends.
- Keywords (whole words, any case), editable; SOS, MAYDAY, EMERGENCY, HELP ME and 911 by default.
- Optionally, detection-sensor messages (off by default: those are usually doors and motion).

Messages from the MeshCore radio get the same bell and keyword checks (check_meshcore).

Alerts heard only through MQTT (the internet) are recorded but not "loud" unless the rules say
so. Loud alerts are the ones the app sounds an alarm for. The rules live in the database, so
the logger and the app agree on them.
"""

import re
import time

DEFAULT_RULES = {
    "enabled": True,
    "keywords": ["SOS", "MAYDAY", "EMERGENCY", "HELP ME", "911"],
    "include_mqtt": False,  # alarm for alerts heard only via the internet, too
    "detection_sensors": False,
}
BELL = "\x07"


def rules(store):
    return {**DEFAULT_RULES, **(store.station("alert_rules") or {})}


def set_rules(store, **changes):
    store.set_station("alert_rules", {**rules(store), **changes})


def keyword_pattern(keyword):
    """Whole-word, case-insensitive; a phrase matches with any spacing between its words."""
    words = keyword.split()
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.IGNORECASE)


def packet_text(packet):
    decoded = packet.get("decoded") or {}
    text = decoded.get("text")
    if text is None and isinstance(decoded.get("payload"), (bytes, bytearray)):
        text = bytes(decoded["payload"]).decode("utf-8", errors="replace")
    return text or ""


def detect(packet, rule_set, local=False):
    """The reason this packet looks like an emergency, or None."""
    if local or not rule_set["enabled"]:
        return None
    decoded = packet.get("decoded") or {}
    port = decoded.get("portnum")
    if decoded.get("emoji"):
        return None  # a reaction, not a message
    if port == "ALERT_APP":
        return "Alert message"
    if port == "DETECTION_SENSOR_APP":
        return "Detection sensor" if rule_set["detection_sensors"] else None
    if port != "TEXT_MESSAGE_APP":
        return None
    return text_reason(packet_text(packet), rule_set)


def text_reason(text, rule_set):
    """Why a text message looks like an emergency (the alert bell, or a keyword), or None."""
    if BELL in text:
        return "Alert bell"
    for keyword in rule_set["keywords"]:
        pattern = keyword_pattern(keyword)
        if pattern is not None and pattern.search(text):
            return f"Keyword: {keyword.upper()}"
    return None


def check_meshcore(store, message_id, text, sender=None, channel=None, channel_name=None, now=None):
    """The same check for a message the MeshCore radio received. MeshCore has no internet
    gateways feeding the mesh the way Meshtastic's MQTT does, so these are always loud."""
    rule_set = rules(store)
    if not rule_set["enabled"]:
        return None
    reason = text_reason(text or "", rule_set)
    if reason is None:
        return None
    where = f"#{channel_name or channel}" if channel is not None else "direct message"
    alert = {
        "at": now or time.time(), "network": "meshcore", "mc_message": message_id, "sender": sender,
        "channel": channel, "portnum": "MESHCORE_TEXT", "reason": f"{reason} (MeshCore {where})",
        "text": (text or "").replace(BELL, "").strip(), "via_mqtt": False, "loud": True,
    }
    alert["id"] = store.record_alert(alert)
    return alert


def check(store, packet, row, local=False, now=None):
    """Record an alert for this packet if it looks like an emergency. Returns the alert or None."""
    rule_set = rules(store)
    reason = detect(packet, rule_set, local)
    if reason is None:
        return None
    via_mqtt = bool(packet.get("viaMqtt"))
    alert = {
        "at": now or time.time(), "packet_row": row, "packet_id": packet.get("id"),
        "from_num": packet.get("from"), "to_num": packet.get("to"), "channel": packet.get("channel", 0),
        "portnum": (packet.get("decoded") or {}).get("portnum"), "reason": reason,
        "text": packet_text(packet).replace(BELL, "").strip(), "via_mqtt": via_mqtt,
        "loud": not via_mqtt or rule_set["include_mqtt"],
    }
    alert["id"] = store.record_alert(alert)
    return alert
