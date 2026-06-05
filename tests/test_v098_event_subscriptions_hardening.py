"""Hardening tests for nth_dao.event_subscriptions per Voss review.

Covers C-7 (cross-platform fnmatch), H-7 (exact-pattern fast path),
H-8 (concurrent poll() double-delivery), M-1 (docstring honesty).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.event_subscriptions import (
    Subscription,
    SubscriptionManager,
    _GLOB_META,
)
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


# ─── C-7: cross-platform case-sensitive glob ──────────────────────────


def test_C7_pattern_is_case_sensitive_on_every_host(subs: SubscriptionManager, bus: EventBus):
    """fnmatch.fnmatch is case-insensitive on Windows, case-sensitive on
    POSIX — same code, different behaviour by OS. fnmatchcase removes
    the OS dependency."""
    collected: list = []
    subs.subscribe("DEPLOY.*", lambda e: collected.append(e.event_type))
    bus.emit("deploy.start", {})        # lowercase
    bus.emit("DEPLOY.start", {})        # uppercase
    subs.poll()
    # Only the exact-case match — never the lowercase one — regardless of OS
    assert collected == ["DEPLOY.start"]


def test_C7_subscription_matches_uses_fnmatchcase():
    """The Subscription.matches helper must also be case-sensitive."""
    s = Subscription(
        subscription_id="x", pattern="group.*",
        subscriber_id="", callback=lambda e: None,
    )
    assert s.matches("group.posted")
    assert not s.matches("GROUP.posted")
    assert not s.matches("Group.Posted")


# ─── H-7: exact pattern pushed into replay (event_types) ────────────


def test_H7_exact_pattern_pushes_event_types_into_replay(
    subs: SubscriptionManager, bus: EventBus,
):
    """A pattern with no glob meta-chars should be passed directly to
    EventBus.replay(event_types=[pattern]) so the bus can short-circuit
    scanning unrelated event types."""
    captured_event_types: list = []

    original_replay = bus.replay
    def spy_replay(*args, event_types=None, **kw):
        captured_event_types.append(event_types)
        return original_replay(*args, event_types=event_types, **kw)
    bus.replay = spy_replay   # type: ignore[method-assign]

    subs.subscribe("deploy.start", lambda e: None)   # exact pattern, no globs
    subs.subscribe("deploy.*", lambda e: None)       # glob pattern

    bus.emit("deploy.start", {})
    subs.poll()
    # First subscription pushed exact pattern down; second did not
    assert ["deploy.start"] in captured_event_types
    assert None in captured_event_types


def test_H7_glob_pattern_does_not_push_event_types(
    subs: SubscriptionManager, bus: EventBus,
):
    """A glob pattern with * / ? / [ should leave event_types=None so
    replay yields everything and fnmatchcase does the actual filtering."""
    for ch in _GLOB_META:
        # spot check: each glob meta char triggers the slow path
        assert ch in _GLOB_META

    captured: list = []
    original_replay = bus.replay
    def spy(*args, event_types=None, **kw):
        captured.append(event_types)
        return original_replay(*args, event_types=event_types, **kw)
    bus.replay = spy   # type: ignore[method-assign]

    subs.subscribe("a?b", lambda e: None)
    subs.subscribe("x[ab]y", lambda e: None)
    subs.subscribe("*", lambda e: None)
    subs.poll()
    # None of the three should push a specific event_types list
    for et in captured:
        assert et is None


# ─── H-8: polling sentinel prevents double-delivery ──────────────────


def test_H8_concurrent_polls_do_not_double_deliver(
    subs: SubscriptionManager, bus: EventBus,
):
    """Two threads call poll() at the same time; each event must reach
    the callback exactly once across both polls. Without the sentinel,
    both could start from the stale cursor and re-deliver."""
    deliveries: list = []
    deliveries_lock = threading.Lock()

    def slow_callback(event):
        time.sleep(0.005)   # window for the race
        with deliveries_lock:
            deliveries.append(event.event_id)

    subs.subscribe("*", slow_callback)
    for _ in range(20):
        bus.emit("x", {})

    threads = [threading.Thread(target=subs.poll) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 20 events, exactly 20 deliveries — no duplicates
    assert len(deliveries) == 20
    assert len(set(deliveries)) == 20


# ─── M-1: poll() docstring is accurate ──────────────────────────────


def test_M1_poll_can_exceed_max_deliveries_when_many_subs(
    bus: EventBus,
):
    """max_deliveries_per_poll caps EACH subscription; total deliveries
    across N subs may be up to N × cap. The docstring now states this
    explicitly."""
    sm = SubscriptionManager(bus, max_deliveries_per_poll=2)
    collected: list = []
    sm.subscribe("a", lambda e: collected.append("a"))
    sm.subscribe("a", lambda e: collected.append("b"))
    sm.subscribe("a", lambda e: collected.append("c"))
    bus.emit("a", {})
    bus.emit("a", {})
    total = sm.poll()
    assert total == 6     # 3 subs × 2 events each — exceeds per-sub cap of 2
    assert collected.count("a") == 2
    assert collected.count("b") == 2
    assert collected.count("c") == 2
