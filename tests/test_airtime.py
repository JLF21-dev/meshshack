"""The airtime gatekeeper's rules, on a fake clock."""

import pytest

from meshshack.airtime import Gatekeeper, Refused
from meshshack.store import Store

BOB, CAROL = 0x0BADBEEF, 0x0C0FFEE0


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def gate(store):
    clock, metrics = Clock(), {}
    g = Gatekeeper(store, metrics=lambda: metrics, clock=clock)
    g.clock_, g.metrics_ = clock, metrics
    return g


def refused(gate, *args, **kwargs):
    with pytest.raises(Refused) as ex:
        gate.authorize(*args, **kwargs)
    return str(ex.value)


def test_kill_switch_blocks_everything_and_persists(gate, store):
    gate.set_transmit_enabled(False)
    assert "turned off" in refused(gate, "manual", "dm", to=BOB)
    assert "turned off" in refused(gate, "manual", "config")
    assert not Gatekeeper(store).transmit_enabled()  # a restarted hub still sees it off
    gate.set_transmit_enabled(True)
    gate.authorize("manual", "dm", to=BOB)


def test_manual_burst_guard(gate):
    for _ in range(5):
        gate.authorize("manual", "dm", to=BOB)
    assert "5 sends in a minute" in refused(gate, "manual", "dm", to=BOB)
    gate.clock_.advance(61)
    gate.authorize("manual", "dm", to=BOB)


def test_traceroutes_are_spaced_and_capped(gate):
    gate.authorize("manual", "traceroute", to=BOB)
    assert "one every 3 minutes" in refused(gate, "manual", "traceroute", to=CAROL)
    for _ in range(5):
        gate.clock_.advance(181)
        gate.authorize("manual", "traceroute", to=CAROL)
    gate.clock_.advance(181)
    assert "6 traceroutes an hour" in refused(gate, "manual", "traceroute", to=CAROL)


def test_busy_channel_warns_you_but_pauses_unattended_senders(gate):
    gate.metrics_["channelUtilization"] = 27.0
    assert "busy" in gate.authorize("manual", "dm", to=BOB).warning
    assert "Paused" in refused(gate, "api:weather", "dm", to=BOB)
    gate.metrics_["channelUtilization"] = 5.0
    gate.metrics_["airUtilTx"] = 6.0
    assert "transmitting a lot" in refused(gate, "api:weather", "dm", to=BOB)


def test_api_budget_broadcast_permission_and_same_node_traceroutes(gate):
    assert "Broadcasts aren't allowed" in refused(gate, "api:tool", "broadcast", channel=0)
    assert "configuration" in refused(gate, "api:tool", "config")
    gate.authorize("api:tool", "traceroute", to=BOB)  # 3 credits
    gate.clock_.advance(31)
    assert "3 minutes" in refused(gate, "api:tool", "traceroute", to=CAROL)
    gate.clock_.advance(200)
    assert "last 3 hours" in refused(gate, "api:tool", "traceroute", to=BOB)
    gate.authorize("api:tool", "broadcast", channel=0, allow_broadcast=True)  # 6 of 6 credits
    gate.clock_.advance(31)
    assert "budget" in refused(gate, "api:tool", "dm", to=BOB)
    gate.authorize("api:other", "dm", to=BOB)  # budgets are per sender
    gate.clock_.advance(3600)
    gate.authorize("api:tool", "dm", to=BOB)


def test_automations_are_rare(gate):
    gate.authorize("automation:time", "broadcast", channel=0, allow_broadcast=True)
    assert "30 s apart" in refused(gate, "automation:weather", "broadcast", channel=0, allow_broadcast=True)
    gate.clock_.advance(3600)
    assert "last 6 hours" in refused(gate, "automation:time", "broadcast", channel=0, allow_broadcast=True)
    for job in ("weather", "alerts", "tides"):
        gate.clock_.advance(60)
        gate.authorize(f"automation:{job}", "dm", to=BOB)
    gate.clock_.advance(6 * 3600)
    assert "daily limit" in refused(gate, "automation:time", "dm", to=BOB)


def test_every_decision_is_logged(gate, store):
    ticket = gate.authorize("manual", "dm", to=BOB)
    gate.sent(ticket, 4242)
    gate.set_transmit_enabled(False)
    refused(gate, "manual", "broadcast", channel=0)
    newest, oldest = store.tx_log()
    assert (oldest["allowed"], oldest["packet_id"], oldest["cost"]) == (1, 4242, 1)
    assert (newest["allowed"], newest["kind"], newest["cost"]) == (0, "broadcast", 3)
    assert "turned off" in newest["reason"]
