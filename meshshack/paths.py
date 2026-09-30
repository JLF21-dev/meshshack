"""How each node's traffic reaches this station: directly, over radio hops, or through the internet.

The firmware flags packets that came from MQTT (the internet), but not every gateway sets that
flag, so internet traffic can arrive looking like ordinary radio traffic. This module also checks
plausibility: a packet whose sender is farther away than its hop count can carry is marked as
*probably* internet ("inferred"), with the reason. Distances are to the nearest point of a rounded
position. The limits allow one exceptional link (SINGLE_LINK_KM, e.g. from a tall site) and a
realistic average per hop across a chain (AVG_KM_PER_HOP); ducting and aircraft can still beat
them, and a node can report a wrong position, so it's always "probably", never certain.

Per node kinds, in the order they win:
  direct      heard straight from it (0 hops, plausible)
  radio       relayed over radio hops, plausibly
  both        radio (direct or relayed) and internet
  mqtt        only through the internet, flagged by the firmware
  inferred    only through the internet as far as the distances show (no flag)
  unknown     nothing to judge by (no hop information: older firmware)
"""

from .gui.common import distance_km, effective_precision, precision_cell

SINGLE_LINK_KM = 100  # one exceptional LongFast link, e.g. between tall sites
AVG_KM_PER_HOP = 50  # a chain's hops average far less across flat terrain (10-40 km is typical)


def max_plausible_km(hops):
    return max(SINGLE_LINK_KM, (hops + 1) * AVG_KM_PER_HOP)

LABELS = {
    "direct": "Direct",
    "radio": "Radio",
    "both": "Radio + internet",
    "mqtt": "Internet",
    "inferred": "Internet?",
    "unknown": "Unknown",
}


def nearest_km(lat0, lon0, lat, lon, bits):
    """Distance to the nearest point where the node could be (its cell, when rounded)."""
    cell = precision_cell(lat, lon, bits)
    if cell is None:
        return distance_km(lat0, lon0, lat, lon)
    (south, west), (north, east) = cell
    return distance_km(lat0, lon0, min(max(lat0, south), north), min(max(lon0, west), east))


def plausible(near_km, hops):
    return near_km is None or near_km <= max_plausible_km(hops)


def summarize(store, station):
    """{node num: {"kind", "label", "why", "direct", "radio", "mqtt", "inferred", "unknown",
    "min_hops", "near_km"}} for every node that sent anything (this station's own reports excluded)."""
    lat0, lon0, _ = station
    positions = {}
    bits_reported = store.position_precision()
    for n in store.nodes():
        if n["latitude"] is not None and lat0 is not None:
            bits = effective_precision(n["latitude"], n["longitude"], bits_reported.get(n["num"]))
            positions[n["num"]] = nearest_km(lat0, lon0, n["latitude"], n["longitude"], bits)
    rows = store._query(
        """SELECT from_num, COALESCE(via_mqtt, 0) != 0 AS mqtt,
                  CASE WHEN hop_start IS NULL OR hop_limit IS NULL THEN NULL ELSE hop_start - hop_limit END AS hops,
                  COUNT(*) AS c
           FROM packets WHERE is_local = 0 AND from_num IS NOT NULL GROUP BY from_num, mqtt, hops""")
    out = {}
    for r in rows:
        num = r["from_num"]
        s = out.setdefault(num, {"direct": 0, "radio": 0, "mqtt": 0, "inferred": 0, "unknown": 0,
                                 "min_hops": None, "implausible_hops": None, "near_km": positions.get(num)})
        near, hops = s["near_km"], r["hops"]
        if r["mqtt"]:
            s["mqtt"] += r["c"]
        elif hops is None:
            s["unknown"] += r["c"]
        elif not plausible(near, hops):
            s["inferred"] += r["c"]
            s["implausible_hops"] = hops if s["implausible_hops"] is None else min(s["implausible_hops"], hops)
        else:
            s["direct" if hops == 0 else "radio"] += r["c"]
            s["min_hops"] = hops if s["min_hops"] is None else min(s["min_hops"], hops)
    for s in out.values():
        s["kind"], s["why"] = _kind(s)
        s["label"] = LABELS[s["kind"]]
        if s["kind"] in ("radio", "both") and s["min_hops"]:
            s["label"] = f"{s['label']} · {s['min_hops']} hop{'s' if s['min_hops'] != 1 else ''}"
    return out


def _kind(s):
    over_air = s["direct"] + s["radio"]
    internet = s["mqtt"] + s["inferred"]
    far = (f"{s['near_km']:.0f} km away in {s['implausible_hops']} hop{'s' if s['implausible_hops'] != 1 else ''}: "
           f"farther than radio carries in that many hops (at most about "
           f"{max_plausible_km(s['implausible_hops']):.0f} km), so probably relayed from the internet without "
           "the MQTT flag, or its position is wrong") if s["inferred"] else None
    if over_air and internet:
        return "both", "Heard over the radio and through the internet" + (f" ({far})" if far else "")
    if s["direct"]:
        return "direct", "Heard straight from it (0 hops)"
    if s["radio"]:
        why = f"Relayed over radio, {s['min_hops']} hop{'s' if s['min_hops'] != 1 else ''} at the fewest"
        if s["near_km"] is not None and s["near_km"] >= 150:
            why += (f"; {s['near_km']:.0f} km away, about {s['near_km'] / (s['min_hops'] + 1):.0f} km a hop: "
                    "possible for a well-sited chain, but long")
        return "radio", why
    if s["mqtt"]:
        return "mqtt", "Only through the internet (MQTT), flagged by the firmware"
    if s["inferred"]:
        return "inferred", far[0].upper() + far[1:]
    return "unknown", "No hop information (older firmware), so the path can't be told"


def packet_path(near_km, hops, via_mqtt):
    """One packet's path: 'mqtt', 'inferred', 'direct', 'radio' or 'unknown'."""
    if via_mqtt:
        return "mqtt"
    if hops is None:
        return "unknown"
    if not plausible(near_km, hops):
        return "inferred"
    return "direct" if hops == 0 else "radio"


MC_LABELS = {"direct": "Direct", "radio": "Radio", "inferred": "Too far?", "unknown": "Unknown",
             "listed": "Not heard"}


def mc_via(node, station):
    """How a MeshCore node reaches this station, from its adverts: (kind, label, why). MeshCore
    positions aren't rounded, but they're whatever the owner typed in, so it's still 'probably'."""
    lat0, lon0, _ = station
    if node["heard_by_us"] is None:
        return "listed", MC_LABELS["listed"], "Only in the radio's contact list; this station hasn't heard it itself"
    hops = node["min_hops"]
    if hops is None:
        return "unknown", MC_LABELS["unknown"], "Heard only by a set route, which doesn't show how far it came"
    near = None
    if lat0 is not None and node["latitude"] is not None:
        near = nearest_km(lat0, lon0, node["latitude"], node["longitude"], None)
    if not plausible(near, hops):
        return "inferred", MC_LABELS["inferred"], (
            f"{near:.0f} km away in {hops} hop{'s' if hops != 1 else ''}: farther than radio carries in that many "
            f"hops (at most about {max_plausible_km(hops):.0f} km), so its position is probably wrong, or a "
            "bridge carried it")
    if hops == 0:
        return "direct", MC_LABELS["direct"], "Heard straight from it (0 hops)"
    return "radio", f"Radio · {hops} hop{'s' if hops != 1 else ''}", \
        f"Relayed by repeaters, {hops} hop{'s' if hops != 1 else ''} at the fewest"
