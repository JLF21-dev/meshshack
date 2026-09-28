"""Formatting helpers shared by the tabs."""

import math
import time

# (label, seconds) choices for "heard within" filters; None means no limit.
WINDOWS = [("Last hour", 3600), ("Last 24 hours", 86400), ("Last 7 days", 7 * 86400), ("All time", None)]


def since_for(seconds):
    return time.time() - seconds if seconds else None


def fmt_ago(ts):
    if not ts:
        return "never"
    secs = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now" if secs < 10 else f"{int(secs)}s ago"


def fmt_clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def fmt_day(ts):
    return time.strftime("%A, %B %-d", time.localtime(ts))


def node_name(row, num=None):
    """Short name if known, else the !id."""
    if row is not None and row["short_name"]:
        return row["short_name"]
    num = row["num"] if row is not None else num
    return f"!{num:08x}" if num is not None else "?"


def distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# ---- position precision ----
# Channels can share positions at reduced precision: the firmware keeps the top N bits of the
# 1e-7-degree latitude/longitude integers and moves the point to the middle of that cell. So a
# rounded position sits exactly on a cell center, which is how we recognize it (and how two
# nodes in the same cell end up at the identical point). Past 24 bits the cell is under ~3 m.
EXACT_FROM_BITS = 25


def infer_precision_bits(lat, lon):
    """The coarsest precision whose cell center is exactly (lat, lon), or None if it looks exact."""
    if lat is None or lon is None:
        return None
    lat_i, lon_i = round(lat * 1e7), round(lon * 1e7)
    for bits in range(1, EXACT_FROM_BITS):
        step = 1 << (32 - bits)
        if (lat_i - step // 2) % step == 0 and (lon_i - step // 2) % step == 0:
            return bits
    return None


def effective_precision(lat, lon, reported_bits=None):
    """Precision bits if the position is rounded, else None. Trusts the packet's own value when given."""
    if reported_bits:
        return reported_bits if reported_bits < EXACT_FROM_BITS else None
    return infer_precision_bits(lat, lon)


def precision_cell(lat, lon, bits):
    """[[south, west], [north, east]] of the area a rounded position stands for, or None if exact."""
    if not bits or bits >= EXACT_FROM_BITS or lat is None or lon is None:
        return None
    step = 1 << (32 - bits)
    south, west = (round(lat * 1e7) // step) * step, (round(lon * 1e7) // step) * step
    return [[south * 1e-7, west * 1e-7], [(south + step) * 1e-7, (west + step) * 1e-7]]


def cell_size_text(lat, bits):
    """'5.8 × 4.4 km': north-south × east-west size of a precision cell at this latitude."""
    step_deg = (1 << (32 - bits)) * 1e-7
    ns, ew = step_deg * 111.32, step_deg * 111.32 * math.cos(math.radians(lat))
    fmt = lambda km: f"{km:.1f} km" if km >= 1 else f"{km * 1000:.0f} m"
    return f"{fmt(ns)} × {fmt(ew)}"


def station_position(status, store, my_num):
    """(lat, lon, precision_bits) for this station, preferring the exact fixed position we set.
    The stored exact position is only used while the radio still reports a fixed position in
    the same precision cell, so a change made elsewhere (e.g. the phone app) isn't masked."""
    pos = status.get("position") or {}
    me = store.node(my_num) if my_num is not None else None
    lat = pos.get("latitude") or (me["latitude"] if me is not None else None)
    lon = pos.get("longitude") or (me["longitude"] if me is not None else None)
    bits = infer_precision_bits(lat, lon)
    exact = store.station("fixed_position")
    if exact and pos.get("fixed_position") and lat is not None:
        same_place = (precision_cell(lat, lon, bits) == precision_cell(exact["latitude"], exact["longitude"], bits)
                      if bits else abs(exact["latitude"] - lat) < 1e-5 and abs(exact["longitude"] - lon) < 1e-5)
        if same_place:
            return exact["latitude"], exact["longitude"], None
    return lat, lon, bits
