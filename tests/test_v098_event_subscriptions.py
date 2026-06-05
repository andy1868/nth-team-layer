"""Tests for nth_dao.event_subscriptions.

The rewrite is ~100 LOC vs the original 384, with per-subscription
cursors (no shared 'advance all' gotcha) and no persistence (callbacks
aren't persistable, so persisting metadata is misleading by design).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.event_subscriptions import Subscription, SubscriptionManager
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bus(tmp_path: Path, alice) -> EventBus:
    return EventBus(tmp_path, identity=alice)


@pytest.fixture
def subs(bus: EventBus) -> SubscriptionManager:
    return SubscriptionManager(bus)


# ─── registry ───────────────────────────────────────────────────────────


def test_subscribe_returns_id(subs: SubscriptionManager):
    sub_id = subs.subscribe("*", lambda e: None, subscriber_id="bob")
    assert isinstance(sub_id, str)
    assert len(sub_id) == 12
    assert subs.subscription(sub_id) is not None


def test_unsubscribe_removes(subs: SubscriptionManager):
    sub_id = subs.subscribe("*", lambda e: None)
    assert subs.unsubscribe(sub_id) is True
    assert subs.unsubscribe(sub_id) is False
    assert subs.subscription(sub_id) is None


def test_list_subscriptions_by_subscriber(subs: SubscriptionManager):
    subs.subscribe("a", lambda e: None, subscriber_id="bob")
    subs.subscribe("b", lambda e: None, subscriber_id="carol")
    bob_subs = subs.list_subscriptions(subscriber_id="bob")
    assert len(bob_subs) == 1
    assert bob_subs[0].subscriber_id == "bob"


def test_subscription_matches_glob():
    s = Subscription(
        subscription_id="x", pattern="group.*",
        subscriber_id="", callback=lambda e: None,
    )
    assert s.matches("group.message.posted")
    assert s.matches("group.task")
    assert not s.matches("mission.step")


# ─── poll: delivery and cursor advancement ─────────────────────────────


def test_poll_delivers_only_matching_events(bus: EventBus, subs: SubscriptionManager):
    collected: list = []
    subs.subscribe("group.*", lambda e: collected.append(e.event_type))
    bus.emit("group.message.posted", {})
    bus.emit("mission.step.completed", {})
    bus.emit("group.task.created", {})
    n = subs.poll()
    assert n == 2
    assert collected == ["group.message.posted", "group.task.created"]


def test_poll_advances_cursor_so_second_poll_is_empty(
    bus: EventBus, subs: SubscriptionManager,
):
    sub_id = subs.subscribe("*", lambda e: None)
    bus.emit("a", {})
    bus.emit("b", {})
    assert subs.poll() == 2
    assert subs.poll() == 0    # cursor advanced; nothing new
    bus.emit("c", {})
    assert subs.poll() == 1


def test_per_subscription_cursors_are_independent(
    bus: EventBus, subs: SubscriptionManager,
):
    """A narrow subscription must not stall when a broad one races ahead."""
    narrow: list = []
    broad: list = []
    subs.subscribe("deploy.*", lambda e: narrow.append(e.event_type))
    subs.subscribe("*", lambda e: broad.append(e.event_type))

    bus.emit("noise.a", {})
    bus.emit("deploy.start", {})
    bus.emit("noise.b", {})
    bus.emit("deploy.finish", {})

    subs.poll()
    assert narrow == ["deploy.start", "deploy.finish"]
    assert broad == ["noise.a", "deploy.start", "noise.b", "deploy.finish"]


def test_unsubscribe_in_middle_of_stream(bus: EventBus, subs: SubscriptionManager):
    """Unsubscribing mid-stream should not deliver stale events on the next poll."""
    collected: list = []
    sub_id = subs.subscribe("*", lambda e: collected.append(e.event_type))
    bus.emit("a", {})
    subs.poll()
    assert collected == ["a"]
    subs.unsubscribe(sub_id)
    bus.emit("b", {})
    assert subs.poll() == 0
    assert collected == ["a"]


def test_start_from_resumes_after_event(bus: EventBus, subs: SubscriptionManager):
    e1 = bus.emit("a", {})
    bus.emit("a", {})
    bus.emit("a", {})
    collected: list = []
    subs.subscribe("a", lambda e: collected.append(e.event_id), start_from=e1.event_id)
    subs.poll()
    # Should get e2 + e3 only, not e1
    assert len(collected) == 2


# ─── failure isolation ───────────────────────────────────────────────


def test_misbehaving_callback_does_not_freeze_others(
    bus: EventBus, subs: SubscriptionManager,
):
    """A subscriber that raises must not stop other subscribers from being notified."""
    bad_called = 0
    good_calls: list = []

    def bad(_event):
        nonlocal bad_called
        bad_called += 1
        raise RuntimeError("intentional")

    subs.subscribe("*", bad)
    subs.subscribe("*", lambda e: good_calls.append(e.event_type))

    bus.emit("a", {})
    bus.emit("b", {})
    subs.poll()
    assert bad_called == 2          # was called for both events
    assert good_calls == ["a", "b"]  # AND the good subscriber still fired


# ─── max_deliveries cap ────────────────────────────────────────────────


def test_max_deliveries_per_poll_bounds_callback_count(bus: EventBus):
    """Callers can cap each poll's work to avoid starving other tasks."""
    capped = SubscriptionManager(bus, max_deliveries_per_poll=2)
    collected: list = []
    capped.subscribe("*", lambda e: collected.append(e.event_id))
    for _ in range(5):
        bus.emit("a", {})
    capped.poll()
    assert len(collected) == 2
    # Next poll picks up where we left off
    capped.poll()
    assert len(collected) == 4
    capped.poll()
    assert len(collected) == 5
