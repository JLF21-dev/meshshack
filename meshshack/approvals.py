"""What other apps may change, and how a change that needs your approval is described to you.

Tokens with the "config" scope can make changes that can't add airtime or expose anything right
away (see IMMEDIATE); everything else they ask for waits in the app for you to approve or deny.
Channel keys, tokens and approvals themselves are never available to other apps.
"""


def describe(path, body, store=None):
    """A plain sentence for the approval list: what exactly would happen."""
    b = body or {}

    def node(num):
        row = store.node(num) if store is not None and isinstance(num, int) else None
        return (row["short_name"] if row is not None and row["short_name"] else None) or (
            f"!{num:08x}" if isinstance(num, int) else str(num))

    if path == "/api/tx":
        return "Turn transmitting ON"
    if path == "/api/reboot":
        return "Reboot the radio"
    if path == "/api/config/owner":
        return f"Rename this node to “{b.get('long_name')}” ({b.get('short_name')})"
    if path == "/api/config/role":
        return f"Change this node's role to {b.get('role')}"
    if path == "/api/config/position":
        fixed = b.get("fixed")
        where = (f"; fixed position {fixed.get('latitude')}, {fixed.get('longitude')}" if isinstance(fixed, dict)
                 else "; fixed position off" if fixed is False else "")
        return (f"Change position settings: broadcast every {b.get('broadcast_secs')} s, smart broadcast "
                f"{'on' if b.get('smart_enabled') else 'off'}, GPS {b.get('gps_mode')}{where}")
    if path == "/api/channels/add":
        return f"Add channel “{b.get('name')}” ({b.get('key', 'random')} key)"
    if path == "/api/channels/update":
        return f"Change channel {b.get('index')}: " + ", ".join(f"{k} → {v}" for k, v in b.items() if k != "index")
    if path == "/api/channels/delete":
        return f"Delete channel {b.get('index')}"
    if path == "/api/automation/save":
        dest = b.get("destination") or {}
        where = "notify only" if dest.get("notify") else f"node {node(dest['to'])}" if "to" in dest else \
            f"channel {dest.get('channel', 0)} (a broadcast)"
        return f"Save automation job “{b.get('name')}” as LIVE: it will transmit to {where}: “{b.get('template')}”"
    if path == "/api/automation/delete":
        return f"Delete automation job {b.get('id')}"
    if path == "/api/automation/settings":
        return f"{'Allow' if b.get('allow_commands') else 'Stop'} automation jobs running local commands"
    return f"{path} {b}"
