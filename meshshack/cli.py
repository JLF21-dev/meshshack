"""meshshack command line: `meshshack run` to log, other subcommands to query the log."""

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from .store import Store

DEFAULT_DB = Path(
    os.environ.get("MESHSHACK_DB")
    or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "meshshack" / "meshshack.db"
)


def parse_since(value):
    """'30m', '24h', '7d' -> unix timestamp that far in the past."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", value.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"expected a duration like 30m, 24h or 7d, got {value!r}")
    seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return time.time() - seconds


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def fmt_ago(ts):
    if not ts:
        return ""
    secs = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return f"{int(secs)}s ago"


def fmt(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def print_table(headers, rows):
    rows = [[fmt(v) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())


# ---- subcommands ---------------------------------------------------------


def cmd_run(args, store):
    from .collector import Collector  # deferred: query commands don't need the radio library

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log = logging.getLogger("meshshack")
    log.info("Logging to %s", args.db)
    store.record_event("started", str(args.db))
    from .events import EventBus

    bus = EventBus()
    collector = Collector(store, port=args.port, retry_seconds=args.retry, bus=bus)

    from .api import ApiServer, Radio
    from .automation import Automation

    radio = Radio(collector, store)  # the one path to the radio, through the airtime gatekeeper
    api = None
    if args.api_port:
        try:
            api = ApiServer(radio, args.db.parent / "hub.json", port=args.api_port, bus=bus)
            api.start()
        except OSError as ex:
            # Keep logging even if the port is taken; the GUI just can't send.
            log.error("API not started on port %s: %s", args.api_port, ex)
            api = None
    automation = Automation(store, radio)
    automation.start()
    try:
        collector.run(stop)
    finally:
        automation.stop()
        if api:
            api.stop()
        store.record_event("stopped")


def cmd_gui(args, store):
    try:
        from .gui.app import run_gui
    except ImportError as ex:
        sys.exit(f"The GUI needs PySide6: .venv/bin/pip install -e '.[gui]'  ({ex})")
    store.close()  # the GUI opens its own connection
    sys.exit(run_gui(args.db, start_hidden=args.tray))


def cmd_nodes(args, store):
    print_table(
        ["id", "short", "long name", "hardware", "role", "last heard", "snr", "rssi", "hops", "batt", "lat", "lon"],
        [
            [
                n["node_id"], n["short_name"], n["long_name"], n["hw_model"], n["role"],
                fmt_ago(n["last_heard"]), n["last_snr"], n["last_rssi"], n["hops_away"],
                f"{n['battery_level']}%" if n["battery_level"] is not None else None,
                f"{n['latitude']:.4f}" if n["latitude"] is not None else None,
                f"{n['longitude']:.4f}" if n["longitude"] is not None else None,
            ]
            for n in store.nodes(since=args.since)
        ],
    )


def cmd_messages(args, store):
    for m in store.messages(limit=args.limit, since=args.since):
        sender = m["from_id"] + (f" ({m['from_short']})" if m["from_short"] else "")
        dest = f"-> {m['to_id']} (DM)" if m["is_direct"] else f"ch{m['channel']}"
        if m["direction"] == "out":
            info = f" [sent, {m['status']}{': ' + m['status_detail'] if m['status_detail'] else ''}]"
        else:
            info = f" [snr {m['rx_snr']}, rssi {m['rx_rssi']}]" if m["rx_snr"] is not None else ""
        print(f"{fmt_time(m['logged_at'])}  {sender} {dest}{info}: {m['text']}")


def cmd_packets(args, store):
    rows = store.packets(limit=args.limit, portnum=args.type, since=args.since)
    if args.json:
        for p in rows:
            print(p["json"])
        return
    print_table(
        ["time", "type", "from", "to", "ch", "snr", "rssi", "hops"],
        [
            [
                fmt_time(p["logged_at"]), p["portnum"],
                (p["from_id"] or "") + (f" ({p['from_short']})" if p["from_short"] else ""),
                p["to_id"], p["channel"], p["rx_snr"], p["rx_rssi"],
                p["hop_start"] - p["hop_limit"] if p["hop_start"] is not None and p["hop_limit"] is not None else None,
            ]
            for p in rows
        ],
    )


def cmd_events(args, store):
    print_table(
        ["time", "event", "detail"],
        [[fmt_time(e["logged_at"]), e["kind"], e["detail"]] for e in store.events(limit=args.limit)],
    )


def cmd_stats(args, store):
    counts, by_port = store.stats(since=args.since)
    for name, count in counts.items():
        print(f"{name:<12} {count}")
    if by_port:
        print()
        print_table(["packet type", "count"], [[r["portnum"], r["c"]] for r in by_port])


def cmd_telemetry(args, store):
    rows = store.telemetry(limit=args.limit, kind=args.kind, since=args.since)
    for t in rows:
        sender = t["from_id"] + (f" ({t['from_short']})" if t["from_short"] else "")
        metrics = json.loads(t["metrics"])
        values = " ".join(f"{k}={fmt(v)}" for k, v in metrics.items())
        print(f"{fmt_time(t['logged_at'])}  {sender}  {t['kind']}: {values}")


def cmd_tx(args, store):
    from .airtime import Gatekeeper

    gate = Gatekeeper(store)
    if args.action in ("on", "off"):
        gate.set_transmit_enabled(args.action == "on", by="command line")
    print(f"Transmitting is {'ON' if gate.transmit_enabled() else 'OFF (kill switch)'}")
    if args.action == "status":
        rows = list(reversed(store.tx_log(limit=args.limit)))
        if rows:
            print()
            print_table(
                ["time", "source", "kind", "to", "ch", "cost", "result", "note"],
                [[fmt_time(r["at"]), r["source"], r["kind"], f"!{r['to_num']:08x}" if r["to_num"] is not None else "",
                  r["channel"], r["cost"], "sent" if r["allowed"] else "REFUSED", r["reason"]] for r in rows],
            )


def cmd_token(args, store):
    if args.action == "create":
        if not args.name:
            sys.exit("usage: meshshack token create NAME [--send] [--allow-broadcast]")
        if args.allow_broadcast and not args.send:
            sys.exit("--allow-broadcast only makes sense with --send")
        scopes = {"read"} | ({"send"} if args.send else set()) | ({"config"} if args.config else set())
        try:
            token = store.create_token(args.name, scopes, allow_broadcast=args.allow_broadcast)
        except ValueError as ex:
            sys.exit(str(ex))
        print(f"Token for {args.name!r} ({', '.join(sorted(scopes))}"
              f"{', broadcasts allowed' if args.allow_broadcast else ''}):\n\n  {token}\n")
        print("It's shown only this once. Use it as:  Authorization: Bearer <token>")
        if args.send:
            print("Sends from this token go through the airtime gatekeeper: 6 credits an hour, "
                  "30 s apart, paused when the channel is busy (see README, Airtime).")
    elif args.action == "revoke":
        if not args.name:
            sys.exit("usage: meshshack token revoke NAME")
        print(f"Revoked {args.name!r}." if store.revoke_token(args.name) else f"No live token named {args.name!r}.")
    else:
        print_table(
            ["name", "scopes", "broadcast", "created", "last used", "status"],
            [[t["name"], t["scopes"], "yes" if t["allow_broadcast"] else "no", fmt_time(t["created_at"]),
              fmt_ago(t["last_used_at"]) or "never", "revoked" if t["revoked_at"] else "active"]
             for t in store.tokens()],
        )


def cmd_radios(args, store):
    from . import devices

    if args.action == "link":
        if not args.hardware_id or not args.kind:
            sys.exit("usage: meshshack radios link HARDWARE_ID --as meshtastic|meshcore")
        devices.link(store, args.hardware_id, args.kind)
        print(f"Linked {devices.hardware_id(args.hardware_id)} as the {devices.KINDS[args.kind]} radio.")
        if args.kind == "meshtastic":
            print("A running logger switches to it within a few seconds.")
        return
    if args.action == "unlink":
        if not args.hardware_id:
            sys.exit("usage: meshshack radios unlink HARDWARE_ID")
        found = devices.unlink(store, args.hardware_id)
        print("Unlinked." if found else "That radio isn't linked.")
        return
    rows = devices.survey(store, probe_ports=args.probe)

    def firmware(r):
        if r.get("in_use"):
            return "(in use)"
        pr = r.get("probe")
        if pr is None:
            return None
        return f"{devices.KINDS[pr['kind']]} {pr['node']}" if pr["kind"] else f"? {pr['error']}"

    print_table(
        ["hardware id", "port", "linked as", "node", "answers as", "device"],
        [[r["hardware_id"], r["port"] or "not plugged in",
          devices.KINDS[r["link"]["kind"]] if r["link"] else None, (r["link"] or {}).get("node"),
          firmware(r), r["description"]] for r in rows],
    )
    if not args.probe:
        print("\n--probe asks each free port what firmware it runs (nothing is transmitted; "
              "some boards restart when their port is opened).")


def cmd_export(args, store):
    from .export import EXPORTS

    fmt = args.format or next(f for (w, f) in EXPORTS if w == args.what)
    entry = EXPORTS.get((args.what, fmt))
    if entry is None:
        choices = ", ".join(f for (w, f) in EXPORTS if w == args.what)
        sys.exit(f"{args.what} can be exported as: {choices}")
    exporter, ext, _ = entry
    if args.out in (None, "-"):
        count = exporter(store, sys.stdout, since=args.since)
    else:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            count = exporter(store, f, since=args.since)
        print(f"Wrote {count} {args.what if count != 1 else args.what.rstrip('s')} to {args.out}", file=sys.stderr)


def cmd_coverage(args, store):
    from .coverage import report, station_from_store

    preset = None
    try:  # the modem preset (for link margins) comes from the running logger, if there is one
        import urllib.request
        info = json.loads((args.db.parent / "hub.json").read_text())
        req = urllib.request.Request(info["url"] + "/api/status", headers={"Authorization": f"Bearer {info['token']}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            preset = (json.loads(resp.read()).get("lora") or {}).get("modem_preset")
    except Exception:
        pass
    rep = report(store, station_from_store(store), preset, since=args.since or 0)
    t = rep["totals"]
    print(f"Heard {t['heard']} packets; {len(rep['neighbors'])} nodes heard directly "
          f"({t['direct'] / (t['heard'] or 1):.0%} of packets)."
          + ("" if preset else " (Link margins need the logger running.)"))
    print()
    print_table(
        ["node", "distance", "bearing", "packets", "median snr", "best", "worst", "margin", "last direct"],
        [[n["short_name"] or n["id"],
          (f"{n['distance_range_km'][0]:.1f}-{n['distance_range_km'][1]:.1f} km" if n["rounded"]
           else f"{n['distance_km']:.1f} km") if n["distance_range_km"] else "",
          n["bearing"], n["packets"], n["snr_median"], n["snr_best"], n["snr_worst"],
          f"{n['margin_db']:+.1f} dB" if n["margin_db"] is not None else "", fmt_ago(n["last_direct"])]
         for n in rep["neighbors"]],
    )
    print()
    print_table(["where traffic comes from", "packets", "share"],
                [[s["label"], s["packets"], f"{s['share']:.1%}"] for s in rep["sources"]])


def cmd_alerts(args, store):
    if args.action == "ack":
        if not args.ids:
            sys.exit("usage: meshshack alerts ack ID [ID ...] | all")
        ids = None if args.ids == ["all"] else [int(i) for i in args.ids]
        print(f"Acknowledged {store.acknowledge_alerts(ids)} alert(s).")
        return
    rows = list(reversed(store.alerts(limit=args.limit, open_only=not args.all)))
    if not rows:
        print("No open alerts." if not args.all else "No alerts.")
        return
    print_table(
        ["id", "time", "from", "reason", "via", "text", "status"],
        [[a["id"], fmt_time(a["at"]), (a["from_short"] or a["from_id"] or ""), a["reason"],
          "MQTT" if a["via_mqtt"] else "radio", a["text"],
          f"acknowledged {fmt_ago(a['acknowledged_at'])}" if a["acknowledged_at"] else "OPEN"] for a in rows],
    )


def cmd_automation(args, store):
    from .automation import describe_trigger

    jobs = store.automation_jobs()
    if not jobs:
        print("No automation jobs. Create them in the app's Automation tab.")
    else:
        print_table(["id", "job", "schedule", "sends to", "mode"],
                    [[j["id"], j["name"], describe_trigger(j["trigger"]),
                      f"!{j['destination']['to']:08x}" if "to" in j["destination"] else f"ch{j['destination']['channel']}",
                      "off" if not j["enabled"] else "dry run" if j["dry_run"] else "LIVE"] for j in jobs])
    runs = list(reversed(store.automation_runs(limit=args.limit)))
    if runs:
        print()
        print_table(["time", "job", "result", "message, or why not"],
                    [[fmt_time(r["at"]), r["job_name"], r["status"],
                      r["text"] if r["status"] in ("sent", "dry run") else r["detail"]] for r in runs])


def build_parser():
    parser = argparse.ArgumentParser(prog="meshshack", description="MeshShack: log, monitor, and control a Meshtastic radio.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"database path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="connect to the radio and log continuously")
    p.add_argument("--port", help="serial device, e.g. /dev/ttyACM0 (default: auto-detect)")
    p.add_argument("--retry", type=int, default=15, help="seconds between reconnect attempts (default: 15)")
    p.add_argument("--api-port", type=int, default=8765,
                   help="localhost port for the desktop app's API; 0 disables it (default: 8765)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging, including the radio library")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("gui", help="open the desktop app (chat, map, nodes, device)")
    p.add_argument("--tray", action="store_true", help="start in the system tray without opening the window")
    p.set_defaults(func=cmd_gui)

    since_help = "only show entries newer than this, e.g. 30m, 24h, 7d"

    p = sub.add_parser("nodes", help="list nodes, most recently heard first")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_nodes)

    p = sub.add_parser("messages", help="show text messages")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("packets", help="show raw packet log")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--type", help="filter by packet type, e.g. POSITION_APP, TELEMETRY_APP")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.add_argument("--json", action="store_true", help="print full packet JSON, one per line")
    p.set_defaults(func=cmd_packets)

    p = sub.add_parser("telemetry", help="show telemetry reports")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--kind", help="e.g. deviceMetrics, environmentMetrics, localStats")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_telemetry)

    p = sub.add_parser("events", help="show connect/disconnect history")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("tx", help="transmit kill switch and log of every send decision")
    p.add_argument("action", nargs="?", choices=["status", "on", "off"], default="status",
                   help="off blocks every transmission (and config change) until turned back on")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_tx)

    p = sub.add_parser("radios", help="list radios on USB, and link them so they're found whichever port they're in")
    p.add_argument("action", nargs="?", choices=["list", "link", "unlink"], default="list")
    p.add_argument("hardware_id", nargs="?", help="as shown by `meshshack radios`")
    p.add_argument("--as", dest="kind", choices=["meshtastic", "meshcore"], help="with link: what the radio is")
    p.add_argument("--probe", action="store_true", help="ask each free port what firmware it runs")
    p.set_defaults(func=cmd_radios)

    p = sub.add_parser("token", help="create, list or revoke tokens for other apps using the local API")
    p.add_argument("action", nargs="?", choices=["list", "create", "revoke"], default="list")
    p.add_argument("name", nargs="?", help="a short name for the app, e.g. weather-display")
    p.add_argument("--send", action="store_true", help="also allow sending (default: read only)")
    p.add_argument("--allow-broadcast", action="store_true",
                   help="with --send: also allow channel broadcasts (default: direct messages only)")
    p.add_argument("--config", action="store_true",
                   help="also allow restricted settings changes: harmless ones right away, the rest only "
                        "after you approve them in the app")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("export", help="export nodes (csv/kml), messages, telemetry (csv) or positions (gpx)")
    p.add_argument("what", choices=["nodes", "messages", "telemetry", "positions"])
    p.add_argument("-f", "--format", choices=["csv", "kml", "gpx"], help="default: csv, or gpx for positions")
    p.add_argument("-o", "--out", help="file to write (default: standard output)")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("coverage", help="who you hear directly (distance, bearing, SNR, margin) and "
                                        "which relays bring you the rest")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("alerts", help="possible emergencies the logger noticed (SOS, MAYDAY, alert messages...)")
    p.add_argument("action", nargs="?", choices=["list", "ack"], default="list")
    p.add_argument("ids", nargs="*", help="with ack: alert ids, or 'all'")
    p.add_argument("--all", action="store_true", help="include acknowledged alerts")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("automation", help="scheduled message jobs and their recent runs")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_automation)

    p = sub.add_parser("stats", help="packet counts by type")
    p.add_argument("--since", type=parse_since, help=since_help)
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not verbose:
        logging.getLogger("meshtastic").setLevel(logging.WARNING)
    store = Store(args.db)
    try:
        args.func(args, store)
    except BrokenPipeError:  # e.g. `meshshack packets | head`
        sys.stderr.close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
