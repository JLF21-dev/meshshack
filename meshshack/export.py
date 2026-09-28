"""Export logged data: nodes as CSV or KML, messages and telemetry as CSV, position history as GPX.

Each exporter writes text to an open file and returns how many records it wrote. Used by
`meshshack export` and the desktop app's Export menu.
"""

import csv
import json
import time
from collections import defaultdict
from xml.sax.saxutils import escape

from .store import path_kind


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def nodes_csv(store, out, since=None):
    via = store.heard_via()
    rows = store.nodes(since=since)
    writer = csv.writer(out)
    writer.writerow(["node_id", "short_name", "long_name", "hw_model", "role", "heard_via", "first_seen",
                     "last_heard", "last_heard_radio", "last_heard_direct", "direct_snr", "direct_rssi",
                     "hops_away", "battery_level", "voltage", "latitude", "longitude", "altitude",
                     "favorite", "ignored"])
    for n in rows:
        writer.writerow([n["node_id"], n["short_name"], n["long_name"], n["hw_model"], n["role"],
                         path_kind(*via.get(n["num"], (0, 0))), _iso(n["first_seen"]), _iso(n["last_heard"]),
                         _iso(n["rf_heard"]), _iso(n["direct_heard"]), n["last_snr"], n["last_rssi"],
                         n["hops_away"], n["battery_level"], n["voltage"], n["latitude"], n["longitude"],
                         n["altitude"], n["is_favorite"], n["is_ignored"]])
    return len(rows)


def nodes_kml(store, out, since=None):
    rows = [n for n in store.nodes(since=since) if n["latitude"] is not None and n["longitude"] is not None]
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n')
    out.write("<name>MeshShack nodes</name>\n")
    for n in rows:
        name = n["short_name"] or n["node_id"]
        desc = f"{n['long_name'] or ''} ({n['node_id']}), {n['hw_model'] or '?'}, last heard {_iso(n['last_heard'])}"
        out.write(f"<Placemark><name>{escape(name)}</name><description>{escape(desc)}</description>"
                  f"<Point><coordinates>{n['longitude']},{n['latitude']},{n['altitude'] or 0}</coordinates>"
                  f"</Point></Placemark>\n")
    out.write("</Document></kml>\n")
    return len(rows)


def messages_csv(store, out, since=None):
    rows = store.messages(limit=10_000_000, since=since)
    writer = csv.writer(out)
    writer.writerow(["time", "direction", "from", "from_name", "to", "channel", "direct", "text", "status",
                     "rx_snr", "rx_rssi"])
    for m in rows:
        writer.writerow([_iso(m["logged_at"]), m["direction"], m["from_id"], m["from_short"], m["to_id"],
                         m["channel"], m["is_direct"], m["text"], m["status"], m["rx_snr"], m["rx_rssi"]])
    return len(rows)


def telemetry_csv(store, out, since=None):
    rows = store.telemetry(limit=10_000_000, since=since)
    keys = sorted({k for t in rows for k in json.loads(t["metrics"])})
    writer = csv.writer(out)
    writer.writerow(["time", "node_id", "short_name", "kind", *keys])
    for t in rows:
        metrics = json.loads(t["metrics"])
        writer.writerow([_iso(t["logged_at"]), t["from_id"], t["from_short"], t["kind"],
                         *(metrics.get(k) for k in keys)])
    return len(rows)


def positions_gpx(store, out, since=None):
    """One GPX track per node, from every position it reported (rounded ones included as sent)."""
    names = {n["num"]: n["short_name"] or n["node_id"] for n in store.nodes()}
    tracks = defaultdict(list)
    for p in store.positions(limit=10_000_000, since=since):
        if p["latitude"] is not None and p["longitude"] is not None:
            tracks[p["from_num"]].append(p)
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<gpx version="1.1" creator="MeshShack" xmlns="http://www.topografix.com/GPX/1/1">\n')
    count = 0
    for num, points in tracks.items():
        out.write(f"<trk><name>{escape(names.get(num) or f'!{num:08x}')}</name><trkseg>\n")
        for p in points:
            ele = f"<ele>{p['altitude']}</ele>" if p["altitude"] is not None else ""
            out.write(f'<trkpt lat="{p["latitude"]}" lon="{p["longitude"]}">{ele}'
                      f"<time>{_iso(p['logged_at'])}</time></trkpt>\n")
            count += 1
        out.write("</trkseg></trk>\n")
    out.write("</gpx>\n")
    return count


# (what, format) -> (exporter, file extension, description)
EXPORTS = {
    ("nodes", "csv"): (nodes_csv, "csv", "Nodes (CSV spreadsheet)"),
    ("nodes", "kml"): (nodes_kml, "kml", "Nodes with a position (KML, e.g. Google Earth)"),
    ("messages", "csv"): (messages_csv, "csv", "Messages (CSV spreadsheet)"),
    ("telemetry", "csv"): (telemetry_csv, "csv", "Telemetry (CSV spreadsheet)"),
    ("positions", "gpx"): (positions_gpx, "gpx", "Position history (GPX tracks)"),
}
