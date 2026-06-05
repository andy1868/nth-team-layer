"""Tests for nth_dao.event_bus correction events.

Correction events are agent-first error patterns layered on top of the
existing append-only hash-chained EventBus. The original event stays in
the stream forever; corrections are NEW events that reference the
original and declare a semantic outcome (DEPRECATED / CORRECTED /
RETRACTED).

Original design contributed by @andy1868 in the agent-collab submission
(June 2026). This suite locks in the integration with NTH DAO's
hash-chained stream, pubkey-based authorisation, payload validation,
and O(1) secondary index.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nth_dao.event_bus import (
    CORRECTION_EVENT_TYPE,
    BusEvent,
    CorrectionType,
    EventBus,
    VerificationResult,
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


# ─── happy path ────────────────────────────────────────────────────────


def test_correct_emits_correction_event_with_reference(bus: EventBus):
    original = bus.emit("group.message.posted",
                        {"channel_id": "general", "body": "deploy v1"})
    correction = bus.correct(
        original.event_id, CorrectionType.CORRECTED,
        reason="wrong version",
        corrected_payload={"body": "deploy v2"},
    )
    assert correction.event_type == CORRECTION_EVENT_TYPE
    assert correction.payload["original_event_id"] == original.event_id
    assert correction.payload["correction_type"] == "CORRECTED"
    assert correction.payload["reason"] == "wrong version"
    assert correction.payload["corrected_payload"] == {"body": "deploy v2"}


def test_correction_chains_into_hash_chain(bus: EventBus):
    e1 = bus.emit("a", {"i": 1})
    e2 = bus.correct(e1.event_id, CorrectionType.CORRECTED, reason="fix")
    e3 = bus.emit("a", {"i": 2})
    assert e2.prev_hash == e1.event_hash
    assert e3.prev_hash == e2.event_hash
    ok, reason = bus.verify_chain()
    assert ok, reason


def test_correction_minimal_call(bus: EventBus):
    original = bus.emit("task.created", {"title": "cleanup"})
    c = bus.correct(original.event_id, CorrectionType.DEPRECATED)
    assert c.payload["correction_type"] == "DEPRECATED"
    assert c.payload["reason"] == ""
    assert "corrected_payload" not in c.payload


def test_correction_signed_by_emitter(bus: EventBus, alice: AgentIdentity):
    e1 = bus.emit("a", {})
    c = bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="compromised")
    assert c.actor_pubkey == alice.pubkey_hex
    assert bus.verify(c) == VerificationResult.VALID


# ─── authorisation: pubkey gate ────────────────────────────────────────


def test_correct_rejects_pubkey_mismatch(bus: EventBus, bob: AgentIdentity):
    """Bob cannot correct an event Alice signed."""
    e1 = bus.emit("a", {"by": "alice"})
    with pytest.raises(ValueError, match="does not match original author"):
        bus.correct(e1.event_id, CorrectionType.RETRACTED, identity=bob)


def test_correct_requires_identity_for_signed_original(tmp_path: Path, alice: AgentIdentity):
    """If the original was signed, an unsigned attempt to correct must fail."""
    signed_bus = EventBus(tmp_path, identity=alice)
    e1 = signed_bus.emit("a", {})
    # New EventBus instance with no identity at all
    unsigned_bus = EventBus(tmp_path)
    with pytest.raises(ValueError, match="no signing identity"):
        unsigned_bus.correct(e1.event_id, CorrectionType.DEPRECATED)


def test_correct_unsigned_original_allows_any_signer(tmp_path: Path, alice: AgentIdentity):
    """If the original was unsigned, any signing identity may correct it."""
    anon_bus = EventBus(tmp_path)            # no identity
    e1 = anon_bus.emit("a", {"by": "anon"})
    assert e1.actor_pubkey == ""

    alice_bus = EventBus(tmp_path, identity=alice)
    c = alice_bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="bogus")
    assert c.actor_pubkey == alice.pubkey_hex


# ─── payload validation ───────────────────────────────────────────────


def test_correct_rejects_payload_with_deprecated(bus: EventBus):
    e1 = bus.emit("a", {})
    with pytest.raises(ValueError, match="only meaningful with CorrectionType.CORRECTED"):
        bus.correct(
            e1.event_id, CorrectionType.DEPRECATED,
            corrected_payload={"x": 1},
        )


def test_correct_rejects_payload_with_retracted(bus: EventBus):
    e1 = bus.emit("a", {})
    with pytest.raises(ValueError, match="only meaningful with CorrectionType.CORRECTED"):
        bus.correct(
            e1.event_id, CorrectionType.RETRACTED,
            corrected_payload={"x": 1},
        )


def test_correct_rejects_bad_event_id_format(bus: EventBus):
    for bad in ("", "short", "NOTHEX0123456789", "0" * 17, "0" * 15):
        with pytest.raises(ValueError, match="must be 16 hex chars"):
            bus.correct(bad, CorrectionType.DEPRECATED)


# ─── retrieval via secondary index ─────────────────────────────────────


def test_get_corrections_for_yields_in_stream_order(bus: EventBus):
    e1 = bus.emit("a", {})
    c1 = bus.correct(e1.event_id, CorrectionType.DEPRECATED, reason="old")
    c2 = bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="bad")
    results = list(bus.get_corrections_for(e1.event_id))
    assert [r.event_id for r in results] == [c1.event_id, c2.event_id]


def test_get_corrections_for_unknown_event_returns_empty(bus: EventBus):
    assert list(bus.get_corrections_for("0123456789abcdef")) == []


def test_get_corrections_falls_back_to_full_scan(bus: EventBus):
    """When the secondary index is missing, slow-path scan still works."""
    e1 = bus.emit("a", {})
    c1 = bus.correct(e1.event_id, CorrectionType.CORRECTED, reason="fix",
                     corrected_payload={"v": 2})
    # Nuke the index — simulates a crash that lost the secondary cache.
    bus.corrections_index_path.unlink()
    results = list(bus.get_corrections_for(e1.event_id))
    assert [r.event_id for r in results] == [c1.event_id]


def test_get_corrections_for_empty_id_short_circuits(bus: EventBus):
    assert list(bus.get_corrections_for("")) == []


# ─── chain integrity preserved ────────────────────────────────────────


def test_correction_does_not_mutate_original(bus: EventBus):
    e1 = bus.emit("a", {"original": True})
    bus.correct(e1.event_id, CorrectionType.RETRACTED, reason="oops")
    reread = bus.get(e1.event_id)
    assert reread is not None
    assert reread.event_id == e1.event_id
    assert reread.event_hash == e1.event_hash
    assert reread.payload == {"original": True}


def test_facade_reexports_correction_type():
    import nth_dao
    assert nth_dao.CorrectionType is CorrectionType
