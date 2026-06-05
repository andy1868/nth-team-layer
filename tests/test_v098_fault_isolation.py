"""Tests for nth_dao.fault_isolation.

Two flaws were fixed vs the original submission by @andy1868:

1. ``AgentHealth._half_open_successes`` was set as a dynamic attribute
   that did not survive persist → reload. Promoted to a real field.

2. State transitions left no signed audit trail, defeating NTH DAO's
   P4 ("signatures not trust assertions"). Optional EventBus
   integration now emits ``circuit.opened`` / ``circuit.half_opened``
   / ``circuit.closed`` events on every transition.

Tests below lock in both fixes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.fault_isolation import (
    AgentHealth,
    CircuitState,
    FaultIsolator,
)
from nth_dao.identity import AgentIdentity, crypto_available


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed audit events")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def fast_isolator(tmp_path: Path) -> FaultIsolator:
    """An isolator with short cooldown so reload tests don't sleep."""
    return FaultIsolator(
        workspace=tmp_path,
        failure_threshold=3,
        failure_window_seconds=60,
        cooldown_seconds=0.05,
        success_threshold=2,
    )


# ─── basic state machine ────────────────────────────────────────────────


def test_initial_state_is_closed(fast_isolator: FaultIsolator):
    assert fast_isolator.circuit_state("bob") == CircuitState.CLOSED.value
    assert not fast_isolator.is_circuit_open("bob")


def test_threshold_opens_circuit(fast_isolator: FaultIsolator):
    for _ in range(3):
        fast_isolator.record_failure("bob", "deploy", "boom")
    assert fast_isolator.is_circuit_open("bob")


def test_cooldown_transitions_to_half_open(fast_isolator: FaultIsolator):
    for _ in range(3):
        fast_isolator.record_failure("bob")
    assert fast_isolator.is_circuit_open("bob")
    time.sleep(0.06)   # > cooldown
    assert fast_isolator.circuit_state("bob") == CircuitState.HALF_OPEN.value
    assert not fast_isolator.is_circuit_open("bob")


def test_half_open_recovery_requires_threshold_successes(
    fast_isolator: FaultIsolator,
):
    for _ in range(3):
        fast_isolator.record_failure("bob")
    time.sleep(0.06)
    fast_isolator.record_success("bob")
    # success_threshold=2 — still HALF_OPEN
    assert fast_isolator.circuit_state("bob") == CircuitState.HALF_OPEN.value
    fast_isolator.record_success("bob")
    # Threshold met — CLOSED
    assert fast_isolator.circuit_state("bob") == CircuitState.CLOSED.value


def test_half_open_probe_failure_reopens(fast_isolator: FaultIsolator):
    # Get to HALF_OPEN
    for _ in range(3):
        fast_isolator.record_failure("bob")
    time.sleep(0.06)
    # One success isn't enough; one failure re-opens
    fast_isolator.record_success("bob")
    fast_isolator.record_failure("bob", error="probe failed")
    fast_isolator.record_failure("bob")
    fast_isolator.record_failure("bob")
    assert fast_isolator.is_circuit_open("bob")


# ─── promoted half_open_successes field SURVIVES reload ───────────────


def test_half_open_successes_survives_persist_reload(tmp_path: Path):
    """Regression for the dynamic-attribute bug.

    The original code set ``_half_open_successes`` as a dynamic attribute
    on the AgentHealth dataclass. asdict() didn't see it, so after a
    persist → reload cycle a partially-recovered HALF_OPEN circuit
    would reset its progress to 0. Now promoted to a real field.
    """
    iso1 = FaultIsolator(
        workspace=tmp_path,
        failure_threshold=3,
        cooldown_seconds=0.01,
        success_threshold=2,
    )
    for _ in range(3):
        iso1.record_failure("bob")
    time.sleep(0.02)
    iso1.record_success("bob")    # half_open_successes -> 1
    h1 = iso1.agent_health("bob")
    assert h1.circuit_state == CircuitState.HALF_OPEN.value
    assert h1.half_open_successes == 1

    # Persist then reload — a fresh instance over the same workspace
    iso2 = FaultIsolator(
        workspace=tmp_path,
        failure_threshold=3,
        cooldown_seconds=0.01,
        success_threshold=2,
    )
    h2 = iso2.agent_health("bob")
    # The progress survived the reload — one more success closes it.
    assert h2.half_open_successes == 1
    iso2.record_success("bob")
    assert iso2.circuit_state("bob") == CircuitState.CLOSED.value


# ─── EventBus audit integration ────────────────────────────────────────


def test_circuit_open_emits_signed_audit_event(tmp_path: Path, alice):
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=3,
    )
    for _ in range(3):
        iso.record_failure("bob", "deploy", "boom")
    events = list(bus.replay(event_types=["circuit.opened"]))
    assert len(events) == 1
    assert events[0].payload["agent_id"] == "bob"
    assert events[0].payload["prev_state"] == "closed"
    assert events[0].payload["reason"] == "threshold_exceeded"
    # Signed because the bus has an identity
    assert events[0].sig


def test_half_open_transition_emits_event(tmp_path: Path, alice):
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=3,
        cooldown_seconds=0.05,
    )
    for _ in range(3):
        iso.record_failure("bob")
    time.sleep(0.06)
    iso.circuit_state("bob")   # trigger lazy transition
    events = list(bus.replay(event_types=["circuit.half_opened"]))
    assert len(events) == 1
    assert events[0].payload["agent_id"] == "bob"
    assert "opened_for_seconds" in events[0].payload


def test_close_emits_event_only_when_state_changed(tmp_path: Path, alice):
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=3,
        cooldown_seconds=0.05,
        success_threshold=2,
    )
    # Open → half-open → close
    for _ in range(3):
        iso.record_failure("bob")
    time.sleep(0.06)
    iso.record_success("bob")
    iso.record_success("bob")
    events = list(bus.replay(event_types=["circuit.closed"]))
    assert len(events) == 1
    assert events[0].payload["reason"] == "half_open_probes_succeeded"

    # Manual reset when already CLOSED — no new emit
    iso.reset("bob")
    events = list(bus.replay(event_types=["circuit.closed"]))
    assert len(events) == 1


def test_manual_reset_emits_event_with_reason(tmp_path: Path, alice):
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=3,
    )
    for _ in range(3):
        iso.record_failure("bob")
    iso.reset("bob")
    closes = list(bus.replay(event_types=["circuit.closed"]))
    assert len(closes) == 1
    assert closes[0].payload["reason"] == "manual_reset"


def test_event_emit_failure_does_not_break_record_path(tmp_path: Path):
    """A downed EventBus must not freeze fault recording — defence in depth."""
    class BrokenBus:
        def emit(self, *a, **kw):
            raise RuntimeError("bus down")
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=BrokenBus(),  # type: ignore[arg-type]
        failure_threshold=2,
    )
    for _ in range(2):
        iso.record_failure("bob")
    # Circuit still opened correctly despite the bus failure
    assert iso.is_circuit_open("bob")


# ─── healthy_agents helper ─────────────────────────────────────────────


def test_healthy_agents_filters_open(fast_isolator: FaultIsolator):
    for _ in range(3):
        fast_isolator.record_failure("bad-agent")
    healthy = fast_isolator.healthy_agents(["good", "bad-agent", "newbie"])
    assert "good" in healthy
    assert "newbie" in healthy   # never seen → assumed healthy
    assert "bad-agent" not in healthy


# ─── schema-tolerant reload ────────────────────────────────────────────


def test_reload_tolerates_unknown_keys(tmp_path: Path):
    """A state file from a future schema bump shouldn't crash reload."""
    iso1 = FaultIsolator(workspace=tmp_path, failure_threshold=2)
    iso1.record_failure("bob")
    iso1.record_failure("bob")

    # Inject an unknown field into the state file
    state = json.loads(iso1._state_path().read_text(encoding="utf-8"))
    state["health"]["bob"]["future_field"] = "from v2.0"
    iso1._state_path().write_text(json.dumps(state), encoding="utf-8")

    iso2 = FaultIsolator(workspace=tmp_path, failure_threshold=2)
    h = iso2.agent_health("bob")  # must not raise
    assert h.failure_count == 2
