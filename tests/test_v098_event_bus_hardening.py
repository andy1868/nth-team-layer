"""Hardening tests for nth_dao.event_bus correction events.

Driven by Dr. Elena Voss's review (C-4, C-5, C-6, M-5, M-7). These
tests pin down behaviours that the original test suite missed and
that would have been exploitable as authorisation bypasses.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nth_dao.event_bus import (
    CORRECTION_EVENT_TYPE,
    BusEvent,
    CorrectionType,
    EventBus,
)
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bob() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob")


@pytest.fixture
def bus(tmp_path: Path, alice) -> EventBus:
    return EventBus(tmp_path, identity=alice)


# ─── C-4: authorisation must verify original's own signature ─────────


def test_C4_correction_refused_when_original_signature_invalid(
    tmp_path: Path, alice, bob,
):
    """Tamper events.jsonl to rewrite actor_pubkey to attacker's key.

    Before the fix, correct() trusted the post-tamper actor_pubkey field
    and let the attacker (bob, now claiming to be alice on disk) retract
    Alice's event. Now correct() calls verify(original) first; tampered
    signature → INVALID → ValueError before any authorisation decision.
    """
    bus = EventBus(tmp_path, identity=alice)
    e1 = bus.emit("ledger.entry", {"value": "important"})
    # Tamper: rewrite actor_pubkey to bob's; signature is now stale and won't
    # verify under bob's key, so the gate must refuse.
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    line = json.loads(lines[0])
    line["actor_pubkey"] = bob.pubkey_hex
    lines[0] = json.dumps(line)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bus.events_dir / "events.index.json"  # not used by tamper

    bob_bus = EventBus(tmp_path, identity=bob)
    with pytest.raises(ValueError, match="signature does not verify"):
        bob_bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="attack")


# ─── C-5: index corruption must not downgrade authorisation ──────────


def test_C5_index_corruption_does_not_allow_unauthorised_correction(
    tmp_path: Path, alice, bob,
):
    """If events.index.json has bogus offsets, get() returns None, but the
    event still exists in events.jsonl. The fix scans the file to confirm
    presence before falling through to the "unsigned original → any signer"
    branch. Without it, Bob could correct Alice's event by corrupting the
    index."""
    bus = EventBus(tmp_path, identity=alice)
    e1 = bus.emit("a", {"by": "alice"})

    # Corrupt the index so get(e1.event_id) returns None
    idx_path = bus.events_dir / "events.index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    idx[e1.event_id] = 99999    # past EOF
    idx_path.write_text(json.dumps(idx), encoding="utf-8")

    assert bus.get(e1.event_id) is None   # confirms the corruption took effect

    # Bob tries to "correct" what the broken index says doesn't exist
    bob_bus = EventBus(tmp_path, identity=bob)
    with pytest.raises(ValueError, match="does not match original author"):
        bob_bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="abuse")


def test_C5_refuses_correction_for_truly_absent_event(bus: EventBus):
    """A correction for an event that genuinely never existed must error
    cleanly, not silently allow any signer through."""
    with pytest.raises(ValueError, match="not found in stream"):
        bus.correct("ffffffffffffffff", CorrectionType.RETRACTED)


# ─── C-6: concurrent corrections preserve every index entry ─────────


def test_C6_concurrent_corrections_do_not_lose_index_entries(
    tmp_path: Path, alice,
):
    """Two threads issue corrections for the same original. Before the
    fix, the read-modify-write of corrections.index.json raced and one
    entry was lost. The fix wraps the RMW in InterProcessLock."""
    bus = EventBus(tmp_path, identity=alice)
    original = bus.emit("a", {})

    results: list = []
    errors: list = []

    def race_correction(reason: str):
        try:
            ev = bus.correct(original.event_id, CorrectionType.DEPRECATED, reason=reason)
            results.append(ev.event_id)
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=race_correction, args=(f"r{i}",))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 8
    # The index must reflect ALL 8 entries — none lost to the race
    listed = [e.event_id for e in bus.get_corrections_for(original.event_id)]
    assert sorted(listed) == sorted(results)


# ─── M-5: refuse to correct a correction ────────────────────────────


def test_M5_cannot_correct_a_correction(bus: EventBus):
    e1 = bus.emit("a", {})
    c1 = bus.correct(e1.event_id, CorrectionType.CORRECTED,
                     reason="fix", corrected_payload={"v": 2})
    with pytest.raises(ValueError, match="cannot correct a correction event"):
        bus.correct(c1.event_id, CorrectionType.RETRACTED, reason="meta")


# ─── M-7: slow path rebuilds and persists the index ─────────────────


def test_M7_slow_path_rebuilds_full_index(bus: EventBus):
    """After the index file is wiped, the first get_corrections_for call
    must rebuild the FULL index, not just resolve the requested key.
    Subsequent calls — even for OTHER originals — should use the fast path."""
    e1 = bus.emit("a", {"i": 1})
    e2 = bus.emit("a", {"i": 2})
    c1 = bus.correct(e1.event_id, CorrectionType.DEPRECATED, reason="old")
    c2 = bus.correct(e2.event_id, CorrectionType.RETRACTED, reason="bad")
    c3 = bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="bad2")

    # Wipe the index — simulates crash that lost the cache
    bus.corrections_index_path.unlink()

    # Slow path call for e1
    list(bus.get_corrections_for(e1.event_id))

    # Index has been rebuilt with BOTH originals' entries
    on_disk = json.loads(bus.corrections_index_path.read_text(encoding="utf-8"))
    assert sorted(on_disk[e1.event_id]) == sorted([c1.event_id, c3.event_id])
    assert on_disk[e2.event_id] == [c2.event_id]


# ─── C-8: timestamps are UTC ────────────────────────────────────────


def test_C8_timestamps_are_utc(bus: EventBus):
    """Local-naive timestamps sort wrong across timezones. Every emitted
    timestamp must carry an explicit timezone offset."""
    event = bus.emit("a", {})
    # ISO-8601 UTC suffix is '+00:00' (or 'Z' but we use +00:00 explicitly)
    assert event.timestamp.endswith("+00:00"), event.timestamp
