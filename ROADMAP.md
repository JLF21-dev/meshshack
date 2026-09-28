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
5. [ ] Automation: scheduled/triggered informational messages (weekly time,
   daily weather), plus emergency detection with loud local alerts; each job
   opt-in, previewed as a dry run first, with a hard minimum interval

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
- [ ] Emergency detection: flag SOS/MAYDAY keywords and alert-bell messages
  with a loud local alert and tray notification (no airtime). Anything that
  would transmit in response needs a human to confirm it.

**Deferred from the API (roadmap step 3)**
- [ ] Optional output to a local MQTT broker (Home Assistant and similar),
  kept separate from the mesh; it never feeds back into it.
- [ ] Manage API tokens from the app (the command line only for now:
  `meshshack token`).
- [ ] A radio layer ready for MeshCore support.
- [ ] A formal API description (OpenAPI).

**Smaller items noted along the way**
- [ ] Database retention/pruning. It grows about 3 MB a day, which is fine
  for now, and the history feeds the charts.
- [ ] Find out why an ESP32-S3 radio once rebooted as the logger opened its
  USB port; later connects didn't reboot it.
- [ ] Map: optional circles instead of boxes was considered and rejected
  (the box is the true area); revisit only if it's asked for.
