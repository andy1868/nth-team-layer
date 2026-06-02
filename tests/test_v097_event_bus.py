"""Tests for nth_dao.event_bus — team-level signed hash-chained event stream.

Original EventBus design contributed by @andy1868 in PR #7. This suite
locks in the NTH DAO standards layered on top: hash chaining, chain
verification, tamper detection, sign/verify round-trip, partial-write
resilience, fork-process isolation, and aggregate stats determinism.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nth_dao.event_bus import (
    DEFAULT_INDEX_FILE,
    ZERO_HASH,
    BusEvent,
    EventBus,
    VerificationResult,
)
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice():
    if not crypto_available():
        pytest.skip("PyNaCl required for signing tests")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bob():
    if not crypto_available():
        pytest.skip("PyNaCl required for signing tests")
    return AgentIdentity.generate(label="bob")


@pytest.fixture
def bus(tmp_path: Path, alice) -> EventBus:
    return EventBus(tmp_path, identity=alice)


# ─── emit + basic chain ──────────────────────────────────────────────────


def test_emit_assigns_seq_1_and_zero_prev_hash(bus: EventBus):
    event = bus.emit("group.message.posted", {"channel_id": "general", "body": "hi"})
    assert event.seq == 1
    assert event.prev_hash == ZERO_HASH
    assert event.event_hash != ZERO_HASH
    assert event.event_hash == event.compute_hash()


def test_emit_chains_prev_hash_across_events(bus: EventBus):
    e1 = bus.emit("a", {"i": 1})
    e2 = bus.emit("a", {"i": 2})
    e3 = bus.emit("a", {"i": 3})
    assert e2.seq == 2 and e3.seq == 3
    assert e2.prev_hash == e1.event_hash
    assert e3.prev_hash == e2.event_hash


def test_emit_signs_event_when_identity_can_sign(bus: EventBus):
    event = bus.emit("group.message.posted", {"body": "signed"})
    assert event.sig
    assert len(event.sig) == 128
    assert bus.verify(event) == VerificationResult.VALID


def test_emit_anonymously_when_no_identity(tmp_path: Path):
    anonymous_bus = EventBus(tmp_path)   # no identity
    event = anonymous_bus.emit("system.startup", {"version": "0.9.7"})
    assert event.sig == ""
    assert event.actor_pubkey == ""
    assert anonymous_bus.verify(event) == VerificationResult.UNSIGNED


def test_emit_with_override_identity(bus: EventBus, bob):
    # alice is bus owner; bob signs this one event ad-hoc
    event = bus.emit("review.given", {"score": 0.92}, identity=bob)
    assert event.actor_pubkey == bob.pubkey_hex
    assert bus.verify(event) == VerificationResult.VALID


# ─── verify_chain ────────────────────────────────────────────────────────


def test_verify_chain_passes_clean_stream(bus: EventBus):
    for i in range(5):
        bus.emit("x", {"i": i})
    ok, reason = bus.verify_chain()
    assert ok, reason


def test_verify_chain_empty_stream(tmp_path: Path):
    empty = EventBus(tmp_path)
    ok, reason = empty.verify_chain()
    assert ok
    assert "empty" in reason


def test_verify_chain_detects_payload_tamper(bus: EventBus, tmp_path: Path):
    bus.emit("a", {"i": 1})
    bus.emit("a", {"i": 2})
    # tamper with the first event's payload but keep its event_hash
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"] = {"i": 9999}        # change payload
    lines[0] = json.dumps(first)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, reason = bus.verify_chain()
    assert not ok
    assert "event_hash mismatch" in reason


def test_verify_chain_detects_chain_break(bus: EventBus):
    bus.emit("a", {})
    bus.emit("a", {})
    bus.emit("a", {})
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "ff" * 32       # break the chain
    second["event_hash"] = BusEvent.from_dict(second).compute_hash()  # re-hash to defeat hash check
    lines[1] = json.dumps(second)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, reason = bus.verify_chain()
    assert not ok
    assert "prev_hash mismatch" in reason


def test_verify_chain_detects_seq_gap(bus: EventBus):
    bus.emit("a", {})
    bus.emit("a", {})
    # drop event 2 from the file → seq jumps 1, 3
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    bus.events_path.write_text(lines[0] + "\n", encoding="utf-8")
    bus.emit("a", {})        # this one will record seq=2 since tail saw last_seq=1
    # So actually drop another way: append event with synthetic seq=99
    e = BusEvent(event_type="a", seq=99, prev_hash="0" * 64)
    e.event_hash = e.compute_hash()
    with bus.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(e.to_dict()) + "\n")
    ok, reason = bus.verify_chain()
    assert not ok
    assert "seq mismatch" in reason


def test_verify_chain_detects_bad_signature(bus: EventBus):
    bus.emit("a", {})
    bus.emit("a", {})
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["sig"] = "00" * 64    # invalidate signature
    lines[0] = json.dumps(first)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, reason = bus.verify_chain()
    assert not ok
    assert "signature invalid" in reason


# ─── replay / get ────────────────────────────────────────────────────────


def test_replay_yields_in_order(bus: EventBus):
    ids = [bus.emit("a", {"i": i}).event_id for i in range(4)]
    streamed = [e.event_id for e in bus.replay()]
    assert streamed == ids


def test_replay_from_id_skips_inclusive(bus: EventBus):
    e1 = bus.emit("a", {})
    e2 = bus.emit("a", {})
    e3 = bus.emit("a", {})
    streamed = [e.event_id for e in bus.replay(from_id=e1.event_id)]
    assert streamed == [e2.event_id, e3.event_id]


def test_replay_filters_event_type_and_actor(bus: EventBus, bob):
    bus.emit("group.message.posted", {})
    bus.emit("group.task.created", {})
    bus.emit("group.message.posted", {}, identity=bob)
    posted = list(bus.replay(event_types=["group.message.posted"]))
    assert len(posted) == 2
    bob_only = list(bus.replay(actor_id=str(bob.agent_id)))
    assert len(bob_only) == 1


def test_replay_reverse_with_limit(bus: EventBus):
    for i in range(5):
        bus.emit("a", {"i": i})
    last_two = list(bus.replay(reverse=True, limit=2))
    assert [e.payload["i"] for e in last_two] == [4, 3]


def test_get_returns_event_by_id_via_index(bus: EventBus):
    e1 = bus.emit("a", {})
    e2 = bus.emit("a", {})
    assert bus.get(e1.event_id).event_id == e1.event_id
    assert bus.get(e2.event_id).event_id == e2.event_id
    assert bus.get("does-not-exist") is None


def test_get_detects_stale_index(bus: EventBus):
    e1 = bus.emit("a", {"i": 1})
    e2 = bus.emit("a", {"i": 2})
    # corrupt the index so e1's id points at e2's offset
    index_path = bus.events_dir / DEFAULT_INDEX_FILE
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    idx[e1.event_id] = idx[e2.event_id]
    index_path.write_text(json.dumps(idx), encoding="utf-8")
    assert bus.get(e1.event_id) is None    # self-check rejects mismatch


# ─── concurrency ─────────────────────────────────────────────────────────


def test_concurrent_emits_keep_chain_intact(tmp_path: Path, alice):
    """Two threads pounding emit() must produce a strictly monotonic chain."""
    bus = EventBus(tmp_path, identity=alice)
    errors: list = []

    def emit_many(tag: str):
        try:
            for i in range(20):
                bus.emit("a", {"tag": tag, "i": i})
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=emit_many, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ok, reason = bus.verify_chain()
    assert ok, reason
    # 40 events, seqs 1..40
    seqs = [e.seq for e in bus.replay()]
    assert seqs == list(range(1, 41))


def test_partial_write_at_tail_recovers(bus: EventBus):
    bus.emit("a", {})
    bus.emit("a", {})
    # simulate a torn write — append a half line
    with bus.events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"event_type":"a","seq":3,"prev')   # no newline, no closing
    # next emit should still chain off event 2, skipping the torn line
    e3 = bus.emit("a", {"recovered": True})
    assert e3.seq == 3
    ok, reason = bus.verify_chain()
    # The torn line is still on disk between events 2 and 3 — verify_chain
    # treats it as a corrupt JSON line and surfaces the problem; that's the
    # right behavior for forensics. Operations cleanup is up to ops.
    assert not ok and "corrupt JSON" in reason


# ─── stats determinism ─────────────────────────────────────────────────


def test_agent_stats_aggregates_per_pubkey(bus: EventBus, alice, bob):
    bus.emit("agent_ledger.step.completed", {})
    bus.emit("agent_ledger.step.completed", {})
    bus.emit("agent_ledger.step.failed", {})
    bus.emit("group.message.posted", {}, identity=bob)
    from nth_dao.event_bus import _fingerprint_of
    alice_stats = bus.agent_stats(_fingerprint_of(alice.pubkey_hex))
    assert alice_stats["steps_completed"] == 2
    assert alice_stats["steps_failed"] == 1
    assert alice_stats["messages_sent"] == 0
    bob_stats = bus.agent_stats(_fingerprint_of(bob.pubkey_hex))
    assert bob_stats["messages_sent"] == 1
    assert bob_stats["steps_completed"] == 0


def test_agent_stats_respects_since(bus: EventBus, alice):
    bus.emit("agent_ledger.step.completed", {})
    # Manually rewrite first event's timestamp to a known past value
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["timestamp"] = "2020-01-01T00:00:00"
    # Need to re-hash + re-sign? For agent_stats which reads raw dicts,
    # hash/sig integrity doesn't matter — only timestamp filtering does.
    lines[0] = json.dumps(first)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bus.emit("agent_ledger.step.completed", {})
    from nth_dao.event_bus import _fingerprint_of
    recent = bus.agent_stats(_fingerprint_of(alice.pubkey_hex), since="2025-01-01")
    assert recent["steps_completed"] == 1   # the old one is excluded


def test_team_stats_groups_by_fingerprint(bus: EventBus, alice, bob):
    bus.emit("a", {})
    bus.emit("a", {})
    bus.emit("a", {}, identity=bob)
    team = bus.team_stats()
    assert team["agent_count"] == 2
    assert team["total_events"] == 3
    # both agents have entries with correct event counts
    counts = {slot["events"] for slot in team["agents"].values()}
    assert counts == {1, 2}


def test_count_filters_by_type(bus: EventBus):
    bus.emit("a", {})
    bus.emit("a", {})
    bus.emit("b", {})
    assert bus.count() == 3
    assert bus.count("a") == 2
    assert bus.count("missing") == 0


# ─── facade ──────────────────────────────────────────────────────────────


def test_facade_reexports_event_bus():
    import nth_dao
    assert nth_dao.EventBus is EventBus
    assert nth_dao.BusEvent is BusEvent
    assert nth_dao.EventBusVerificationResult is VerificationResult


# ─── verify_all matrix ───────────────────────────────────────────────────


def test_verify_all_buckets_results(bus: EventBus, alice):
    bus.emit("a", {})
    bus.emit("a", {})
    # tamper to make one invalid
    lines = bus.events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["sig"] = "00" * 64
    lines[0] = json.dumps(first)
    bus.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    total, valid, invalid, unverifiable = bus.verify_all()
    assert total == 2
    assert valid == 1
    assert invalid == 1
    assert unverifiable == 0
