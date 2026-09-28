"""Coverage analysis: how this station hears the mesh, from what's already logged (nothing is sent).

- Direct neighbors: nodes heard straight from the source (0 hops, not MQTT), with distance and
  bearing from this station, their SNR spread, and the link margin above the modem preset's
  decoding limit. Positions shared at reduced precision give a distance *range*, not a number.
- Traffic sources: what share of everything heard arrived directly, through each relaying
  neighbor, via MQTT, or by an unknown path. The firmware only records the last byte of the
  relaying node's ID, so a relay is matched to known nodes by that byte (and flagged when that
  matches more than one).
"""

import math
import statistics
import time

from .gui.common import distance_km, effective_precision, precision_cell

# Spreading factor of each modem preset, and the lowest SNR (dB) LoRa can decode at each SF
# (Semtech SX126x datasheet). A link's margin is its SNR above that limit.
PRESET_SF = {
    "SHORT_TURBO": 7, "SHORT_FAST": 7, "SHORT_SLOW": 8, "MEDIUM_FAST": 9, "MEDIUM_SLOW": 10,
    "LONG_FAST": 11, "LONG_MODERATE": 11, "LONG_SLOW": 12, "VERY_LONG_SLOW": 12,
}
SF_SNR_LIMIT = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def snr_limit(preset):
    sf = PRESET_SF.get(preset or "")
    return SF_SNR_LIMIT.get(sf)


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg):
    return COMPASS[round(deg / 22.5) % 16]


def distance_range_km(lat1, lon1, lat2, lon2, bits):
    """(nearest, farthest) distance from point 1 to where point 2 could be. For a rounded
    position that's the distance to the nearest and farthest points of its precision cell."""
    cell = precision_cell(lat2, lon2, bits)
    if cell is None:
        d = distance_km(lat1, lon1, lat2, lon2)
        return d, d
    (south, west), (north, east) = cell
    near_lat, near_lon = min(max(lat1, south), north), min(max(lon1, west), east)
    corners = [(south, west), (south, east), (north, west), (north, east)]
    return (distance_km(lat1, lon1, near_lat, near_lon),
            max(distance_km(lat1, lon1, la, lo) for la, lo in corners))


def relay_candidates(byte, direct_nums, known_nums):
    """Nodes whose ID ends in `byte`. A relay is heard directly by definition, so direct
    neighbors are preferred; otherwise any known node might be it."""
    direct = sorted(n for n in direct_nums if n & 0xFF == byte)
    return direct or sorted(n for n in known_nums if n & 0xFF == byte)


def station_from_store(store):
    """This station's (lat, lon, precision bits) without the radio: the exact fixed position the
    app stored, else the position of the node that sent this station's own (local) reports."""
    fixed = store.station("fixed_position")
    if fixed:
        return fixed["latitude"], fixed["longitude"], None
    rows = store._query("SELECT from_num FROM packets WHERE is_local = 1 GROUP BY from_num ORDER BY COUNT(*) DESC LIMIT 1")
    me = store.node(rows[0]["from_num"]) if rows else None
    if me is not None and me["latitude"] is not None:
        return me["latitude"], me["longitude"], effective_precision(me["latitude"], me["longitude"])
    return None, None, None


def report(store, station, preset, since=0):
    """station: (lat, lon, precision bits or None) of this station, or (None, None, None).
    Returns {"neighbors": [...], "sources": [...], "totals": {...}, "snr_limit": dB or None}."""
    data = store.coverage(since)
    nodes = {n["num"]: n for n in store.nodes()}
    bits_reported = store.position_precision()
    lat0, lon0, _ = station
    limit = snr_limit(preset)

    neighbors = []
    for num, d in data["direct"].items():
        n = nodes.get(num)
        median = statistics.median(d["snrs"])
        row = {
            "num": num, "id": f"!{num:08x}", "short_name": n["short_name"] if n else None,
            "long_name": n["long_name"] if n else None, "packets": len(d["snrs"]),
            "snr_median": median, "snr_best": max(d["snrs"]), "snr_worst": min(d["snrs"]),
            "rssi_median": statistics.median(d["rssis"]) if d["rssis"] else None,
            "last_direct": d["last"], "margin_db": median - limit if limit is not None else None,
            "latitude": None, "longitude": None, "distance_km": None, "distance_range_km": None,
            "bearing_deg": None, "bearing": None, "rounded": False,
        }
        if n is not None and n["latitude"] is not None and lat0 is not None:
            bits = effective_precision(n["latitude"], n["longitude"], bits_reported.get(num))
            near, far = distance_range_km(lat0, lon0, n["latitude"], n["longitude"], bits)
            deg = bearing_deg(lat0, lon0, n["latitude"], n["longitude"])
            row.update(latitude=n["latitude"], longitude=n["longitude"], rounded=bool(bits),
                       distance_km=distance_km(lat0, lon0, n["latitude"], n["longitude"]),
                       distance_range_km=[near, far], bearing_deg=deg, bearing=compass(deg))
        neighbors.append(row)
    neighbors.sort(key=lambda r: -r["packets"])

    totals = data["totals"]
    heard = totals["heard"] or 1
    sources = [{"kind": "direct", "label": "Heard directly", "packets": totals["direct"]}]
    direct_nums = set(data["direct"])
    for byte, count in sorted(data["relays"].items(), key=lambda kv: -kv[1]):
        byte = int(byte)
        cands = relay_candidates(byte, direct_nums, nodes)
        names = [(nodes[c]["short_name"] if c in nodes and nodes[c]["short_name"] else f"!{c:08x}") for c in cands]
        # Unsure when the byte matches several nodes, or only nodes never heard directly.
        sure = len(cands) == 1 and cands[0] in direct_nums
        sources.append({"kind": "relay", "byte": byte, "candidates": cands, "packets": count, "certain": sure,
                        "label": (f"Via {names[0]}" if len(names) == 1 else
                                  f"Via {' or '.join(names)}" if names else "Via an unknown relay")
                        + ("" if sure or not names else "?") + f" (ID ends 0x{byte:02x})"})
    sources.append({"kind": "mqtt", "label": "Via the internet (MQTT)", "packets": totals["mqtt"]})
    sources.append({"kind": "unknown", "label": "Path unknown (older firmware)", "packets": totals["unknown_path"]})
    for s in sources:
        s["share"] = s["packets"] / heard
    return {"neighbors": neighbors, "sources": [s for s in sources if s["packets"]],
            "totals": totals, "snr_limit": limit, "generated_at": time.time()}
