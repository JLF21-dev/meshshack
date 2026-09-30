# MeshShack roadmap

Where MeshShack is headed and what's left to do. Items are ticked as they land.

Ground rule: be extremely careful about putting traffic on the mesh. No
auto-replies to incoming messages. Automation means scheduled or triggered
sends of genuinely useful information (e.g. a weekly NIST time check, a daily
weather report) and emergency handling, each strictly rate-limited. Every
transmission goes through one airtime gatekeeper, with a kill switch.

1. [x] Foundations: honest signal columns (direct vs. relayed/MQTT), tag the
   radio's local USB telemetry, save traceroute/request results, fix the
   close-time error
2. [x] Airtime gatekeeper: weighted budgets, back-off on busy channel, kill
   switch, audit log (before any new sending path)
3. [x] API for other apps: read endpoints, live event stream, scoped tokens
   with per-token airtime budgets (deferred parts are in the backlog below)
4. [x] Everyday features: channel management with QR, favorites/ignore, node
   detail panel, telemetry charts, export; also the tray icon, start at
   login, and the logger as an always-on service
   - [ ] Remote admin of own nodes: design agreed, on hold until the solar
     repeater node is built (then build and test against it)
5. [x] Automation: scheduled/triggered informational messages (weekly time,
   daily weather), plus emergency detection with loud local alerts; each job
   opt-in, previewed as a dry run first, with a hard minimum interval.
   Messages are user-defined; their content can come from data sources and
   APIs (see "Automation design" below)

Paused until a solar repeater node is available to build and test remote admin against.

## Backlog (future development)

Picked up when work resumes. The ground rule above applies to every item.

**Next, once the solar node exists**
- [ ] Remote admin of own nodes (roadmap step 4). The agreed design:
  - Setup is done by hand: add this station's public key
    (shown in the app, with a copy button) to the target's
    Security → Admin keys.
  - Narrow first version: read status/config; set owner, role and position
    interval; reboot; reset the node list.
  - Manual only, through the gatekeeper and kill switch, a confirmation
    showing exactly what's sent on every write, and targets within 2 hops
    only. Reading full config is about 8–10 small exchanges; count them.
  - Test against the solar repeater first, then a handheld.
- [ ] Settle the repeater's role with the local group (REPEATER is
  deprecated; ROUTER_LATE is one current option) and its broadcast intervals
  (a fixed node: position every 12–24 h, telemetry every 1–2 h).

**Automation (roadmap step 5)**
- [ ] Scheduler for informational messages: a weekly NIST/local time check,
  a daily weather report. Each job is off until enabled, dry-run first, has
  random timing jitter, and is sent through the gatekeeper (each job at most
  every 6 h, 4 automated sends a day in total, paused above 20% channel
  utilization). No auto-replies to incoming messages.
- [x] Emergency detection: SOS/MAYDAY and other keywords, alert messages and the
  alert bell raise a loud local alert and tray notification (no airtime). Anything
  that would transmit in response needs a human to confirm it.

**Automation design (roadmap step 5)**

The goal: define your own automated messages in the app, with content filled
in at send time from data sources, including external APIs. Proposed shape:

- [x] **Jobs** you create in an Automation tab: a name; a trigger; a
  destination (a channel or a direct message); a message template; and on/off.
  Each new job starts in dry-run mode.
- [x] **Schedules:** weekly on a day and time, daily at a time, or every N
  hours (never under 6 h), with a few minutes of random jitter per run.
- [x] **Templates** with placeholders, e.g. `{time}`, `{date}`,
  `{station.channel_util}`, `{node.CMP.battery}`, `{weather.temp}`. There's a
  live preview that renders the real text and its byte count against the
  200-byte limit; a message that renders too long is refused, not truncated.
- [x] **Data sources** that fill placeholders:
  - built-in: local time and a NIST/NTP clock-offset check, this station's
    telemetry, mesh stats from the log (nodes heard, busiest hour, …);
  - HTTP/JSON APIs: a URL, fields picked by path (e.g. the National Weather
    Service API for a forecast), a timeout, and a cache so a schedule
    doesn't hammer the API; failures skip the send and log why;
  - a local command's output (opt-in, for your own scripts and sensors);
  - other apps can already send through the API with a `--send` token, and
    get the same gatekeeper limits.
- [x] **Safety:** every automated send goes through the gatekeeper's
  automation limits (each job at most every 6 h, 4 automated sends a day in
  total, paused above 20% channel utilization, 30 s apart); no auto-replies
  to incoming messages; a per-job log of what was sent or skipped, and why.

- [x] Event triggers: a node (or any favorite) going quiet or coming back, a
  low battery, the channel staying busy; once per change; "notify me" by
  default (nothing sent); busy-channel jobs can only notify.

**Other apps (added after step 3)**
- [x] An Other apps tab: approvals, tokens, recent app activity.
- [x] A restricted settings permission: harmless changes right away, everything else only
  after you approve it in the app; keys, tokens and approvals never exposed.
- [x] Every node marked Direct / Radio / Internet (flagged) / Internet? (inferred from
  distance vs hops) / both / unknown, everywhere.

**Deferred from the API (roadmap step 3)**
- [ ] Optional output to a local MQTT broker (Home Assistant and similar),
  kept separate from the mesh; it never feeds back into it.
- [x] Manage API tokens from the app (Device tab), as well as `meshshack token`.
- [x] Radios found by hardware ID, with Scan and Link (Device tab, `meshshack radios`).

**MeshCore** (a second radio running MeshCore companion firmware, alongside Meshtastic)
- [x] Step 1, receive only: log contacts, adverts and heard packets (own tables, keyed by
  public key); MeshCore nodes on the map (Meshtastic / MeshCore / Both) and in Nodes.
- [ ] Step 2: MeshCore chat, read only: Public, hashtag channels, direct messages, kept
  separate from Meshtastic conversations.
- [ ] Step 3: sending, manual only, through the airtime gatekeeper with its own budget;
  the kill switch covers both radios.
- Never a full bridge between the meshes. Maybe later: automation that relays only
  emergency traffic (SOS/HELP keywords, chosen senders) from one mesh to the other,
  opt-in, dry run first, rate-limited, loop-proof, and labeled with where it came from.
- [ ] A formal API description (OpenAPI).

**Ideas**
- [x] Coverage analysis: SNR against distance, bearings, link margins, and which relays carry your traffic.
- [x] Coverage over time: how direct links and relay shares change, with notes to mark changes (e.g. after moving an antenna).
- [ ] Export to InfluxDB/Grafana.
- [ ] Device settings, stage 2: region and modem preset (with strong warnings).
- [x] Replies and emoji reactions from the chat view.

**Smaller items noted along the way**
- [ ] Database retention/pruning. It grows about 3 MB a day, which is fine
  for now, and the history feeds the charts.
- [ ] Find out why an ESP32-S3 radio once rebooted as the logger opened its
  USB port; later connects didn't reboot it.
- [ ] Map: optional circles instead of boxes was considered and rejected
  (the box is the true area); revisit only if it's asked for.
