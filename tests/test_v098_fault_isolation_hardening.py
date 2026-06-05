"""Hardening tests for nth_dao.fault_isolation per Voss review.

Covers H-1 (lock layering), H-10 (write amplification), H-11
(unbounded failures list), M-6 (broad except), and the persistence
tolerance for corrupt entries surfaced during the rewrite.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus
from nth_dao.fault_isolation import (
    CircuitState,
    FaultIsolator,
)
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


# ─── H-1: EventBus emit happens OUTSIDE the FaultIsolator lock ────────


def test_H1_emit_outside_lock_does_not_deadlock(tmp_path: Path, alice):
    """If EventBus.emit() calls back into FaultIsolator (e.g. a future
    consumer that records bus health), holding self._lock across emit
    would deadlock. The fix queues events and drains after release."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(workspace=tmp_path, event_bus=bus, failure_threshold=3)
    # Reentrant callback simulating a downstream consumer that records
    # something about its own activity. With the old code this would
    # have deadlocked because record_failure held self._lock while
    # emit() was called.
    original_emit = bus.emit
    def reentrant_emit(event_type, payload, **kw):
        # Touch the isolator AGAIN — would deadlock under the old
        # within-lock emit
        iso.health_score("bob")
        return original_emit(event_type, payload, **kw)
    bus.emit = reentrant_emit   # type: ignore[method-assign]

    for _ in range(3):
        iso.record_failure("bob")
    # Must reach here without hanging
    assert iso.is_circuit_open("bob")


# ─── H-10: persist only on TRANSITIONS, not on every record ──────────


def test_H10_persist_is_skipped_for_non_transition_records(
    tmp_path: Path, alice,
):
    """Non-transitioning record_success / record_failure calls must NOT
    rewrite the state file. The write count should be bounded by the
    number of state transitions, not the number of events."""
    bus = EventBus(tmp_path, identity=alice)
    iso = FaultIsolator(
        workspace=tmp_path, event_bus=bus,
        failure_threshold=5,
    )
    state_path = iso._state_path()

    # 1 first failure → triggers initial state creation, dirty=False (not a
    # transition), but file may not exist yet so first call writes nothing.
    iso.record_failure("bob")
    # Without transition, file should NOT exist
    assert not state_path.exists()

    # 4 more failures, still under threshold → still no persist
    for _ in range(3):
        iso.record_failure("bob")
    assert not state_path.exists()

    # 5th failure crosses threshold → STATE TRANSITION → persist
    iso.record_failure("bob")
    assert state_path.exists()


def test_H10_flush_writes_dirty_state(tmp_path: Path, alice):
    """flush() should force-write any pending in-memory state at
    shutdown — used for graceful exit."""
    iso = FaultIsolator(workspace=tmp_path, failure_threshold=10)
    iso.record_failure("bob")     # below threshold, not dirty
    # Even after flush, no transition → no write
    iso.flush()
    # But after a transition, flush rewrites cleanly
    for _ in range(10):
        iso.record_failure("bob")
    iso.flush()
    assert iso._state_path().exists()


# ─── H-11: failures list is bounded ────────────────────────────────────


def test_H11_failures_deque_is_bounded(tmp_path: Path):
    """A flood of failures must not grow memory unbounded. The deque
    capacity is max(failure_threshold * 4, 32) — the floor of 32 prevents
    pathologically small buffers when threshold is configured low."""
    iso = FaultIsolator(
        workspace=tmp_path,
        failure_threshold=5,
        failure_window_seconds=3600,   # long so nothing prunes
    )
    for _ in range(1000):
        iso.record_failure("flood")
    failures = iso._failures["flood"]
    assert isinstance(failures, deque)
    expected_cap = max(5 * 4, 32)
    assert failures.maxlen == expected_cap
    assert len(failures) <= expected_cap


def test_H11_expired_failures_pruned_from_left(tmp_path: Path):
    """Old failures outside the window should be popped during
    _recent_failures, not held forever."""
    iso = FaultIsolator(
        workspace=tmp_path,
        failure_threshold=5,
        failure_window_seconds=0.05,   # 50ms window
    )
    iso.record_failure("transient")
    iso.record_failure("transient")
    assert len(iso._failures["transient"]) == 2
    time.sleep(0.06)
    iso.record_failure("transient")   # triggers prune via _recent_failures
    # Only the most recent failure remains
    assert len(iso._failures["transient"]) == 1


# ─── M-6: narrower exception catch in event emit path ────────────────


def test_M6_emit_swallows_OSError_but_propagates_logic_bugs(
    tmp_path: Path,
):
    """A disk-full or file-lock OSError on emit must not break the
    fault path. But a programmer error (TypeError, AttributeError)
    should propagate so the bug surfaces — the original broad
    `except Exception` hid both."""
    class FlakyBus:
        def __init__(self): self.calls = 0
        def emit(self, *a, **kw):
            self.calls += 1
            raise OSError("disk full")
    iso = FaultIsolator(
        workspace=tmp_path / "flaky", event_bus=FlakyBus(),  # type: ignore[arg-type]
        failure_threshold=2,
    )
    # OSError swallowed — circuit still opens correctly
    for _ in range(2):
        iso.record_failure("bob")
    assert iso.is_circuit_open("bob")

    class BuggyBus:
        def emit(self, *a, **kw):
            raise TypeError("missing required positional argument: 'event_type'")
    # SEPARATE workspace so iso2 doesn't inherit the OPEN state iso left
    # behind on disk — otherwise no NEW transition fires, no emit attempt,
    # no TypeError to catch.
    iso2 = FaultIsolator(
        workspace=tmp_path / "buggy", event_bus=BuggyBus(),  # type: ignore[arg-type]
        failure_threshold=2,
    )
    # TypeError is a programmer error — propagate so the bug surfaces
    with pytest.raises(TypeError):
        for _ in range(2):
            iso2.record_failure("bob")


# ─── corrupt persisted state tolerance ──────────────────────────────


def test_corrupt_health_entry_dropped_not_crashed(tmp_path: Path):
    """A corrupt entry in fault_state.json (e.g. produced by a future
    schema where a required field was renamed) should be DROPPED with a
    warning, not crash _ensure_loaded."""
    iso1 = FaultIsolator(workspace=tmp_path, failure_threshold=2)
    iso1.record_failure("bob")
    iso1.record_failure("bob")   # transition → persists

    # Corrupt the persisted state — 'bob' entry has wrong type
    import json
    raw = json.loads(iso1._state_path().read_text(encoding="utf-8"))
    raw["health"]["bob"] = "not a dict"
    raw["health"]["alice"] = {"agent_id": "alice"}
    iso1._state_path().write_text(json.dumps(raw), encoding="utf-8")

    # Fresh isolator reloads — must not crash, must keep usable agents
    iso2 = FaultIsolator(workspace=tmp_path, failure_threshold=2)
    iso2._ensure_loaded()
    assert "bob" not in iso2._health     # corrupt entry dropped
    assert "alice" in iso2._health       # valid entry kept
