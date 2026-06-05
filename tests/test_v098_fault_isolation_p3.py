"""P3: failures emit signed audit events; counter rebuildable across restarts.

H-10's lazy persistence (transitions only) meant an attacker who could
crash the service after each failure-batch would evade the threshold
counter - the counter only persists when it crosses. P3 fixes this by
emitting a signed ``failure.observed`` event for EVERY failure, and
providing ``replay_from_event_bus()`` to rebuild the in-memory window
on startup from those signed events.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.fault_isolation import CircuitState, FaultIsolator
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


# ===== failure.observed emission =====


def test_P3_every_failure_emits_signed_audit_event(tmp_path: Path, alice):
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path, event_bus=bus, failure_threshold=10,
    )
    for i in range(5):
        iso.record_failure("bob", action_type="deploy", error=f"err{i}")

    events = list(bus.replay(event_types=["failure.observed"]))
    assert len(events) == 5
    for ev in events:
        assert ev.payload["agent_id"] == "bob"
        assert ev.payload["action_type"] == "deploy"
        assert ev.sig   # signed


def test_P3_failure_event_below_threshold_still_recorded(tmp_path: Path, alice):
    """Critical regression: before P3, threshold-relative failures were
    invisible on the audit stream. An attacker who reset after every
    batch < threshold would leave NO trace at all."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path, event_bus=bus, failure_threshold=10,
    )
    for _ in range(3):
        iso.record_failure("bob")
    iso.flush()
    # Below threshold -> no circuit.opened
    opened = list(bus.replay(event_types=["circuit.opened"]))
    assert len(opened) == 0
    # But P3 says each failure still leaves a signed observation
    observed = list(bus.replay(event_types=["failure.observed"]))
    assert len(observed) == 3


# ===== replay_from_event_bus rebuild =====


def test_P3_restart_rebuilds_counter_from_event_stream(tmp_path: Path, alice):
    """The full attack scenario: 4 failures recorded, threshold is 5,
    attacker crashes the process. A naive FaultIsolator restart sees
    counter = 0 and the 5th failure barely trips the breaker. With
    P3, replay_from_event_bus() reconstructs all 4 prior failures, and
    the 5th immediately opens the circuit."""
    bus1 = EventBus(tmp_path, identity=alice)
    iso1 = FaultIsolator(
        workspace=tmp_path, event_bus=bus1, failure_threshold=5,
        failure_window_seconds=3600,
    )
    for _ in range(4):
        iso1.record_failure("bob")
    iso1.flush()
    # 4 failures, no transition yet
    assert not iso1.is_circuit_open("bob")

    # Restart - fresh isolator over the same workspace
    bus2 = EventBus(tmp_path, identity=alice)
    iso2 = FaultIsolator(
        workspace=tmp_path, event_bus=bus2, failure_threshold=5,
        failure_window_seconds=3600,
    )
    # Without P3, this would be the post-restart state: counter = 0,
    # so 1 more failure does NOT trip. With P3:
    replayed = iso2.replay_from_event_bus()
    assert replayed == 4

    # Now the 5th failure should immediately open
    iso2.record_failure("bob")
    assert iso2.is_circuit_open("bob")


def test_P3_replay_respects_failure_window(tmp_path: Path, alice):
    """Failures older than failure_window must NOT be replayed - they're
    outside the time window the breaker cares about."""
    import time
    bus = EventBus(tmp_path, identity=alice)
    iso1 = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=10, failure_window_seconds=0.05,
    )
    iso1.record_failure("bob")
    iso1.flush()
    time.sleep(0.08)   # > window

    iso2 = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=10, failure_window_seconds=0.05,
    )
    # The aged failure is outside the window; replay should skip it
    replayed = iso2.replay_from_event_bus()
    assert replayed == 0


def test_P3_replay_is_idempotent(tmp_path: Path, alice):
    """Multiple calls to replay_from_event_bus() must not double-count."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=10, failure_window_seconds=3600,
    )
    for _ in range(3):
        iso.record_failure("bob")
    iso.flush()

    n1 = iso.replay_from_event_bus()
    n2 = iso.replay_from_event_bus()
    assert n1 == 3
    assert n2 == 3
    # And the in-memory deque has exactly 3, not 6 or 9
    assert len(iso._failures["bob"]) == 3


def test_P3_replay_no_op_without_event_bus(tmp_path: Path):
    """No event_bus -> nothing to replay; returns 0 gracefully."""
    iso = FaultIsolator(workspace=tmp_path, failure_threshold=5)
    assert iso.replay_from_event_bus() == 0


def test_P3_replay_tolerates_malformed_events(tmp_path: Path, alice):
    """An attacker who can corrupt the EventBus payload of a failure
    event must not crash the replay path. Skip + continue, count valid
    entries."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=10, failure_window_seconds=3600,
    )
    # 1 real failure event
    iso.record_failure("good")
    iso.flush()
    # Inject malformed failure events directly to the bus
    bus.emit("failure.observed", {})   # missing agent_id
    bus.emit("failure.observed", {"agent_id": "x"})   # missing timestamp
    bus.emit("failure.observed", {"agent_id": "y", "timestamp": "garbage"})

    iso2 = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=10, failure_window_seconds=3600,
    )
    # Only the well-formed one counts
    assert iso2.replay_from_event_bus() == 1
