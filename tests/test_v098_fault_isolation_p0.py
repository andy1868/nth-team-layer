"""P0-#3: fault_isolation pending event snapshot+clear must be atomic.

Original code (in _drain_pending_events) had a comment claiming the
snapshot was 'under a brief lock', but no lock was actually taken.
A thread appending to _pending_events between list() and clear()
would have its event silently dropped.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.fault_isolation import FaultIsolator
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


def test_P0_3_concurrent_drain_does_not_drop_events(tmp_path: Path, alice):
    """Two threads simultaneously cause state transitions while a third
    is mid-drain. Every transition must produce a corresponding signed
    event on the bus; none silently dropped between snapshot and clear."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=2,
        cooldown_seconds=0.001,   # short so half-opens happen fast
        success_threshold=1,
    )

    # 20 distinct agents -> each gets pushed CLOSED -> OPEN, then we
    # immediately recover them. Total expected emits = 20 opens + 20
    # half_opens + 20 closes = 60.
    agent_ids = [f"agent{i}" for i in range(20)]

    def punish_then_recover(aid):
        iso.record_failure(aid)
        iso.record_failure(aid)   # transition -> OPEN
        time.sleep(0.01)          # cooldown
        iso.is_circuit_open(aid)  # triggers HALF_OPEN
        iso.record_success(aid)   # success_threshold=1 -> CLOSED

    threads = [threading.Thread(target=punish_then_recover, args=(a,))
               for a in agent_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    iso.flush()    # ensure any tail in _pending_events is emitted

    opened = list(bus.replay(event_types=["circuit.opened"]))
    half = list(bus.replay(event_types=["circuit.half_opened"]))
    closed = list(bus.replay(event_types=["circuit.closed"]))

    # Strict counts: nothing dropped, nothing duplicated
    assert len(opened) == 20, f"opens lost: {len(opened)}/20"
    assert len(half) == 20, f"half_opens lost: {len(half)}/20"
    assert len(closed) == 20, f"closes lost: {len(closed)}/20"


def test_P0_3_drain_under_concurrent_append_keeps_audit_complete(
    tmp_path: Path, alice,
):
    """Pathological case: thread A starts drain while thread B is
    inside record_failure with its own pending event queued. The fix
    serialises drain's snapshot+clear with B's defer step."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path,
        event_bus=bus,
        failure_threshold=1,   # every failure transitions immediately
    )

    barrier = threading.Barrier(50)
    errors: list = []

    def fire(i):
        try:
            barrier.wait()
            iso.record_failure(f"agent{i}")
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    iso.flush()
    assert not errors
    # 50 first-failures with threshold=1 -> 50 circuit.opened events.
    # Without the lock fix, racy drains would drop a stochastic subset.
    opened = list(bus.replay(event_types=["circuit.opened"]))
    assert len(opened) == 50
