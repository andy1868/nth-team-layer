"""Tests for correction events — agent-only error-handling pattern on EventBus.

Corrections are NOT message-delete/recall — those are human social UX.
Agents don't make typos; they make deterministic mistakes (wrong port,
stale URL, compromised credential). The correction pattern is: emit a
new ``event.correction`` event that references the original, declares
a correction type (DEPRECATED / CORRECTED / RETRACTED), and optionally
carries a corrected payload. The original event stays in the stream;
audit integrity is preserved.
"""

from pathlib import Path
from typing import Optional

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


def _raw_events(bus: EventBus) -> list[dict]:
    """Return raw dicts from the stream for payload inspection."""
    return list(bus._stream_raw())


# ─── correct() ────────────────────────────────────────────────────────


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


def test_correct_fails_with_invalid_event_id(bus: EventBus):
    """Caller must provide a real original_event_id (basic validation)."""
    with pytest.raises(ValueError, match="original_event_id"):
        bus.correct("", CorrectionType.DEPRECATED)


def test_correct_signs_when_identity_can_sign(bus: EventBus):
    """Correction inherits emitter identity, just like emit()."""
    original = bus.emit("group.message.posted", {"body": "x"})
    c = bus.correct(original.event_id, CorrectionType.CORRECTED,
                    reason="typo")

    assert c.actor_id  # non-empty — agent_id is hex fingerprint
    if bus.can_sign:
        assert len(c.sig) == 128
        assert bus.verify(c) != VerificationResult.INVALID


def test_correct_unsigned_for_anonymous_bus(tmp_path: Path):
    """Anonymous bus can still correct — corrections are protocol, not
    just signed-identity actions."""
    anonymous = EventBus(tmp_path)
    e1 = anonymous.emit("group.message.posted", {"body": "x"})
    c = anonymous.correct(e1.event_id, CorrectionType.DEPRECATED)

    assert c.actor_id == ""
    assert c.sig == ""


# ─── get_corrections_for() ────────────────────────────────────────────


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


# ─── CorrectionType enum ──────────────────────────────────────────────


def test_correction_type_enum_values():
    """Three standard correction types — extensible but not arbitrary."""
    assert CorrectionType.DEPRECATED.value == "DEPRECATED"
    assert CorrectionType.CORRECTED.value == "CORRECTED"
    assert CorrectionType.RETRACTED.value == "RETRACTED"


# ─── integration: verify_chain with corrections ───────────────────────


def test_verify_chain_passes_with_correction_events(bus: EventBus):
    """Corrections are legitimate chain members; verify_chain must pass."""
    original = bus.emit("group.message.posted", {"body": "v1"})
    bus.correct(original.event_id, CorrectionType.CORRECTED,
                reason="fix", corrected_payload={"body": "v2"})
    bus.emit("group.message.posted", {"body": "v3"})

    ok, reason = bus.verify_chain()
    assert ok, reason
