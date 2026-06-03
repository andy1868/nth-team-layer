"""Tests for correction events — agent-only error-handling pattern on EventBus.

Corrections are NOT message-delete/recall — those are human social UX.
Agents don't make typos; they make deterministic mistakes (wrong port,
stale URL, compromised credential). The correction pattern is: emit a
new ``event.correction`` event that references the original, declares
a correction type (DEPRECATED / CORRECTED / RETRACTED), and optionally
carries a corrected payload. The original event stays in the stream;
audit integrity is preserved.

Authorisation: only the original emitter (matched by Ed25519 pubkey)
may correct a signed event. Unsigned events may be corrected by any
signed identity.
"""

from pathlib import Path

import pytest

from nth_dao.event_bus import (
    BusEvent,
    CorrectionType,
    EventBus,
    VerificationResult,
)
from nth_dao.identity import AgentIdentity


@pytest.fixture
def alice() -> AgentIdentity:
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bob() -> AgentIdentity:
    return AgentIdentity.generate(label="bob")


@pytest.fixture
def bus(tmp_path: Path, alice: AgentIdentity) -> EventBus:
    return EventBus(tmp_path, identity=alice)


# ─── correct() — happy path ────────────────────────────────────────────


def test_correct_emits_correction_event_with_reference(bus: EventBus):
    """correct() emits an event.correction with the original event_id."""
    original = bus.emit("group.message.posted",
                        {"channel_id": "general", "body": "deploy v1"})
    c = bus.correct(original.event_id, CorrectionType.CORRECTED,
                    reason="wrong version",
                    corrected_payload={"body": "deploy v2"})

    assert c.event_type == "event.correction"
    assert c.payload["original_event_id"] == original.event_id
    assert c.payload["correction_type"] == "CORRECTED"
    assert c.payload["reason"] == "wrong version"
    assert c.payload["corrected_payload"] == {"body": "deploy v2"}


def test_correct_links_into_hash_chain(bus: EventBus):
    """Correction events chain normally — no special indexing needed."""
    e1 = bus.emit("group.message.posted", {"body": "a"})
    e2 = bus.correct(e1.event_id, CorrectionType.CORRECTED, reason="fix")
    e3 = bus.emit("group.message.posted", {"body": "b"})

    assert e2.prev_hash == e1.event_hash
    assert e3.prev_hash == e2.event_hash


def test_correct_accepts_minimal_call(bus: EventBus):
    """Only original_event_id and type are required."""
    original = bus.emit("task.created", {"title": "cleanup"})
    c = bus.correct(original.event_id, CorrectionType.DEPRECATED)

    assert c.event_type == "event.correction"
    assert c.payload["original_event_id"] == original.event_id
    assert c.payload["correction_type"] == "DEPRECATED"
    assert c.payload["reason"] == ""


def test_correct_deprecated_with_reason_no_payload(bus: EventBus):
    """DEPRECATED is valid with just a reason, no corrected_payload."""
    original = bus.emit("group.message.posted", {"body": "old-deploy-url"})
    c = bus.correct(original.event_id, CorrectionType.DEPRECATED,
                    reason="deploy URL rotated")
    assert c.payload["correction_type"] == "DEPRECATED"
    assert c.payload["reason"] == "deploy URL rotated"
    assert "corrected_payload" not in c.payload


def test_correct_signs_when_identity_can_sign(bus: EventBus):
    """Correction inherits emitter identity, just like emit()."""
    original = bus.emit("group.message.posted", {"body": "x"})
    c = bus.correct(original.event_id, CorrectionType.CORRECTED,
                    reason="typo")

    assert c.actor_id  # non-empty — agent_id is hex fingerprint
    if bus.can_sign:
        assert len(c.sig) == 128
        assert bus.verify(c) != VerificationResult.INVALID


# ─── correct() — validation ────────────────────────────────────────────


def test_correct_rejects_malformed_event_id(bus: EventBus):
    """Must be exactly 16 hex chars — no whitespace, no uppercase, no
    path traversal attacks, no random strings."""
    for bad in ["", "   ", "abc", "a" * 15, "a" * 17, "not-a-hex-id!!!",
                "../../../etc/passwd", "ZZZZZZZZZZZZZZZZ"]:
        with pytest.raises(ValueError, match="original_event_id"):
            bus.correct(bad, CorrectionType.DEPRECATED)


def test_correct_rejects_corrected_payload_with_deprecated(bus: EventBus):
    """DEPRECATED + corrected_payload → ValueError."""
    original = bus.emit("group.message.posted", {"body": "x"})
    with pytest.raises(ValueError, match="corrected_payload"):
        bus.correct(original.event_id, CorrectionType.DEPRECATED,
                    corrected_payload={"body": "y"})


def test_correct_rejects_corrected_payload_with_retracted(bus: EventBus):
    """RETRACTED + corrected_payload → ValueError."""
    original = bus.emit("group.message.posted", {"body": "x"})
    with pytest.raises(ValueError, match="corrected_payload"):
        bus.correct(original.event_id, CorrectionType.RETRACTED,
                    corrected_payload={"body": "y"})


# ─── correct() — authorisation ─────────────────────────────────────────


def test_correct_same_emitter_can_correct_own_event(bus: EventBus, alice: AgentIdentity):
    """Alice corrects Alice's event → allowed."""
    original = bus.emit("group.message.posted", {"body": "x"})
    c = bus.correct(original.event_id, CorrectionType.CORRECTED,
                    reason="fix", identity=alice)
    assert c.event_type == "event.correction"


def test_correct_rejects_cross_agent_correction(
    bus: EventBus, bob: AgentIdentity
):
    """Bob cannot correct Alice's signed event."""
    original = bus.emit("group.message.posted", {"body": "alice's msg"})
    with pytest.raises(ValueError, match="does not match original author"):
        bus.correct(original.event_id, CorrectionType.RETRACTED, identity=bob)


def test_correct_rejects_anonymous_correction_of_signed_event(bus: EventBus):
    """No identity → cannot correct a signed event."""
    original = bus.emit("group.message.posted", {"body": "signed"})
    anonymous_bus = EventBus(bus.workspace)  # same workspace, no identity
    with pytest.raises(ValueError, match="no signing identity"):
        anonymous_bus.correct(original.event_id, CorrectionType.DEPRECATED)


def test_correct_allows_any_signer_for_unsigned_event(
    tmp_path: Path, bob: AgentIdentity
):
    """Unsigned events have no author — any signer can correct them."""
    anonymous = EventBus(tmp_path)
    original = anonymous.emit("group.message.posted", {"body": "anon"})

    # Bob (different bus, same workspace) can correct it
    bob_bus = EventBus(tmp_path, identity=bob)
    c = bob_bus.correct(original.event_id, CorrectionType.DEPRECATED)
    assert c.actor_id
    assert c.event_type == "event.correction"


def test_correct_nonexistent_event_still_allowed(bus: EventBus, alice: AgentIdentity):
    """Correcting an event that doesn't exist yet (valid 16-hex id but
    not in stream) is allowed — the original may be in a different
    replica or not yet synced. get() returns None, so authorisation
    check is skipped (no pubkey to match)."""
    fake_id = "a1b2c3d4e5f67890"
    c = bus.correct(fake_id, CorrectionType.DEPRECATED, reason="pre-emptive")
    assert c.event_type == "event.correction"
    assert c.payload["original_event_id"] == fake_id


# ─── get_corrections_for() ─────────────────────────────────────────────


def test_get_corrections_for_returns_related_corrections(bus: EventBus):
    """Multiple corrections for one original — all returned in order."""
    original = bus.emit("group.message.posted", {"body": "v1"})
    c1 = bus.correct(original.event_id, CorrectionType.CORRECTED,
                     reason="first fix")
    bus.emit("group.message.posted", {"body": "unrelated"})
    c2 = bus.correct(original.event_id, CorrectionType.CORRECTED,
                     reason="second fix")

    corrections = list(bus.get_corrections_for(original.event_id))
    assert len(corrections) == 2
    assert corrections[0].event_id == c1.event_id
    assert corrections[1].event_id == c2.event_id


def test_get_corrections_for_uses_fast_path_index(bus: EventBus):
    """After correct(), the secondary index is populated so lookups
    are O(1) and don't scan the full stream."""
    original = bus.emit("group.message.posted", {"body": "x"})
    bus.correct(original.event_id, CorrectionType.CORRECTED, reason="fix")

    cidx = bus._load_corrections_index()
    assert original.event_id in cidx
    assert len(cidx[original.event_id]) == 1

    # get_corrections_for should still return the same result
    corrections = list(bus.get_corrections_for(original.event_id))
    assert len(corrections) == 1


def test_get_corrections_for_returns_empty_when_none(bus: EventBus):
    """Uncorrected event → empty iterator."""
    original = bus.emit("group.message.posted", {"body": "fine"})
    bus.emit("group.message.posted", {"body": "also fine"})

    corrections = list(bus.get_corrections_for(original.event_id))
    assert corrections == []


def test_get_corrections_for_only_matches_correction_type(bus: EventBus):
    """Other event types mentioning an event_id in payload are NOT
    returned — only event.correction events count."""
    original = bus.emit("group.message.posted", {"body": "x"})
    bus.emit("custom.reference", {"original_event_id": original.event_id})

    corrections = list(bus.get_corrections_for(original.event_id))
    assert corrections == []


# ─── correction-of-correction ──────────────────────────────────────────


def test_correction_of_correction_is_allowed(bus: EventBus):
    """A correction can itself be corrected — agents make mistakes
    even when fixing mistakes."""
    original = bus.emit("group.message.posted", {"body": "v1"})
    c1 = bus.correct(original.event_id, CorrectionType.CORRECTED,
                     reason="first fix", corrected_payload={"body": "v2"})

    # Correct the correction (e.g. wrong fix)
    c2 = bus.correct(c1.event_id, CorrectionType.CORRECTED,
                     reason="fix was wrong too", corrected_payload={"body": "v3"})

    assert c2.event_type == "event.correction"
    assert c2.payload["original_event_id"] == c1.event_id

    # Both corrections are findable independently
    assert len(list(bus.get_corrections_for(original.event_id))) == 1
    assert len(list(bus.get_corrections_for(c1.event_id))) == 1


# ─── CorrectionType enum ───────────────────────────────────────────────


def test_correction_type_enum_values():
    """Three standard correction types — extensible but not arbitrary."""
    assert CorrectionType.DEPRECATED.value == "DEPRECATED"
    assert CorrectionType.CORRECTED.value == "CORRECTED"
    assert CorrectionType.RETRACTED.value == "RETRACTED"


# ─── integration: verify_chain with corrections ────────────────────────


def test_verify_chain_passes_with_correction_events(bus: EventBus):
    """Corrections are legitimate chain members; verify_chain must pass."""
    original = bus.emit("group.message.posted", {"body": "v1"})
    bus.correct(original.event_id, CorrectionType.CORRECTED,
                reason="fix", corrected_payload={"body": "v2"})
    bus.emit("group.message.posted", {"body": "v3"})

    ok, reason = bus.verify_chain()
    assert ok, reason


# ─── corrections index rebuild ─────────────────────────────────────────


def test_rebuild_corrections_index_recovers_from_loss(bus: EventBus):
    """_rebuild_corrections_index() scans the stream and reconstructs
    the index from scratch."""
    original = bus.emit("group.message.posted", {"body": "x"})
    c1 = bus.correct(original.event_id, CorrectionType.CORRECTED,
                     reason="fix 1")
    c2 = bus.correct(original.event_id, CorrectionType.CORRECTED,
                     reason="fix 2")

    # Rebuild
    cidx = bus._rebuild_corrections_index()
    assert original.event_id in cidx
    assert cidx[original.event_id] == [c1.event_id, c2.event_id]

    # get_corrections_for should still work
    corrections = list(bus.get_corrections_for(original.event_id))
    assert len(corrections) == 2


def test_rebuild_corrections_index_handles_empty_stream(bus: EventBus):
    """Rebuild on empty stream → empty dict, no crash."""
    cidx = bus._rebuild_corrections_index()
    assert cidx == {}
