"""G-13 (Voss audit): safe_append_jsonl_batch — batched JSONL append.

Calling safe_append_jsonl N times means N lock acquisitions, N file
opens, N fsyncs. For hot-path callers (event-bus drains, transcript
dumps), that's both slow AND weaker semantically: a concurrent
reader holding the lock between writes can see a partial batch.

safe_append_jsonl_batch:
  * one lock acquisition for the whole batch
  * one file open
  * one fsync
  * validate-all-before-write so a mid-batch encode failure leaves
    the file untouched (vs. the N-individual-call pattern, which
    would leave the first K-1 records committed)

Pinned invariants:
  * Empty iterable -> no-op, no lock taken, no file touched, returns 0
  * Multi-record batch appears as a single atomic block on disk
  * Returns the count of records actually appended
  * Mid-batch encode failure aborts the whole batch (no partial write)
  * Embedded newline in any record aborts the whole batch
  * fsync runs exactly once per batch
  * external_lock_held respected (no inner lock taken)
  * Concurrent batches do not interleave records
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nth_dao.util import (
    LOCK_TIMEOUT_DEFAULT,
    LOCK_TIMEOUT_PATIENT,
    safe_append_jsonl_batch,
)


# ===== empty / single-record =====


def test_G13_empty_iterable_is_a_noop(tmp_path):
    """No records -> no lock, no file, returns 0."""
    target = tmp_path / "events.jsonl"
    n = safe_append_jsonl_batch(target, [])
    assert n == 0
    assert not target.exists()
    # And the .locks/ dir should also be untouched
    assert not (tmp_path / ".locks").exists()


def test_G13_single_record_writes_one_line(tmp_path):
    """Single-record batch is equivalent to a single safe_append_jsonl
    call - same output, same lock semantics."""
    target = tmp_path / "events.jsonl"
    n = safe_append_jsonl_batch(target, [{"msg": "first"}])
    assert n == 1
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"msg": "first"}


# ===== multi-record happy path =====


def test_G13_multi_record_batch_writes_all_records(tmp_path):
    """Three records, three lines, in order."""
    target = tmp_path / "events.jsonl"
    records = [{"i": 0}, {"i": 1}, {"i": 2}]
    n = safe_append_jsonl_batch(target, records)
    assert n == 3
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed == records


def test_G13_batch_appended_after_existing_data_preserves_prior(tmp_path):
    """Batch APPEND - existing lines must not be lost / overwritten."""
    target = tmp_path / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"existing": 1}\n', encoding="utf-8")
    safe_append_jsonl_batch(target, [{"new": 1}, {"new": 2}])
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"existing": 1}
    assert json.loads(lines[1]) == {"new": 1}
    assert json.loads(lines[2]) == {"new": 2}


# ===== validate-all-before-write =====


def test_G13_record_producing_literal_newline_aborts_whole_batch(tmp_path, monkeypatch):
    """The defensive literal-newline check guards against custom JSON
    encoders that emit raw newlines (json.dumps's default escapes
    them, so we simulate a misbehaving custom encoder by patching).
    A bad record mid-batch must abort the WHOLE batch - the
    validate-all-before-write contract."""
    import nth_dao.util.jsonl_safe as mod
    real_dumps = mod.json.dumps

    def evil_dumps(record, **kw):
        # Simulate a custom encoder that leaks a literal newline
        # for one specific record.
        if record.get("trigger") == "bomb":
            return '{"bomb": "with\nliteral_newline"}'
        return real_dumps(record, **kw)

    monkeypatch.setattr(mod.json, "dumps", evil_dumps)

    target = tmp_path / "events.jsonl"
    with pytest.raises(ValueError, match="literal newline"):
        safe_append_jsonl_batch(target, [
            {"good": "first"},
            {"trigger": "bomb"},
            {"good": "third"},
        ])
    # File must NOT exist (or must be empty) - no partial batch
    assert not target.exists() or target.read_text(encoding="utf-8") == ""


def test_G13_non_serialisable_record_aborts_whole_batch(tmp_path):
    """If a record contains a non-JSON-encodable value, the whole
    batch is rejected with TypeError and the file is untouched."""
    class Unencodable:
        pass

    target = tmp_path / "events.jsonl"
    with pytest.raises(TypeError):
        safe_append_jsonl_batch(target, [
            {"good": 1},
            {"bad": Unencodable()},  # json.dumps will TypeError
            {"good": 2},
        ])
    assert not target.exists() or target.read_text(encoding="utf-8") == ""


# ===== external_lock_held semantics =====


def test_G13_external_lock_held_skips_inner_lock(tmp_path):
    """When the caller is inside a wider transaction (e.g. credit
    ledger flush), batch must NOT take its own lock - same contract
    as the single-record helper."""
    target = tmp_path / "events.jsonl"
    n = safe_append_jsonl_batch(
        target, [{"x": 1}, {"x": 2}],
        external_lock_held=True,
    )
    assert n == 2
    # No .locks/ created by us
    assert not (tmp_path / ".locks").exists()


# ===== concurrency =====


def test_G13_concurrent_batches_do_not_interleave(tmp_path):
    """Two threads each appending a 50-record batch under the same
    lock must NOT interleave records. Each batch is a single atomic
    block in the output."""
    target = tmp_path / "events.jsonl"
    batch_a = [{"src": "a", "i": i} for i in range(50)]
    batch_b = [{"src": "b", "i": i} for i in range(50)]

    barrier = threading.Barrier(2)

    def writer(batch):
        barrier.wait()
        safe_append_jsonl_batch(target, batch)

    t1 = threading.Thread(target=writer, args=(batch_a,))
    t2 = threading.Thread(target=writer, args=(batch_b,))
    t1.start(); t2.start(); t1.join(); t2.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    parsed = [json.loads(line) for line in lines]

    # Find where the 'a' block and 'b' block live; each must be
    # a contiguous run of 50 (no interleaving inside a batch).
    sources = [r["src"] for r in parsed]
    # First run length:
    first_src = sources[0]
    first_run = 0
    while first_run < len(sources) and sources[first_run] == first_src:
        first_run += 1
    # If batches are atomic, the first 50 are one source and the
    # next 50 are the other.
    assert first_run == 50
    assert all(s == first_src for s in sources[:50])
    other_src = "b" if first_src == "a" else "a"
    assert all(s == other_src for s in sources[50:])


# ===== timeout tier integration =====


def test_G13_batch_accepts_timeout_tier_constants(tmp_path):
    """Batch should accept the same LOCK_TIMEOUT_* tier constants as
    the single-record helper - shared timeout vocabulary."""
    target = tmp_path / "events.jsonl"
    n = safe_append_jsonl_batch(
        target, [{"tier": "patient"}],
        lock_timeout=LOCK_TIMEOUT_PATIENT,
    )
    assert n == 1


# ===== fsync runs once =====


def test_G13_batch_fsync_runs_at_most_once(tmp_path, monkeypatch):
    """Performance promise: batch should fsync at most once, not
    once per record. Patch os.fsync and count calls."""
    import nth_dao.util.jsonl_safe as mod
    calls = []
    real_fsync = mod.os.fsync

    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", counting_fsync)

    target = tmp_path / "events.jsonl"
    safe_append_jsonl_batch(
        target, [{"i": i} for i in range(10)],
    )
    # 10 records, but the batch helper should call fsync once.
    assert len(calls) == 1


def test_G13_batch_with_fsync_disabled_calls_fsync_zero_times(tmp_path, monkeypatch):
    """fsync=False completely skips fsync - useful for callers who
    flush asynchronously."""
    import nth_dao.util.jsonl_safe as mod
    calls = []
    real_fsync = mod.os.fsync

    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", counting_fsync)

    target = tmp_path / "events.jsonl"
    safe_append_jsonl_batch(
        target, [{"i": i} for i in range(5)], fsync=False,
    )
    assert calls == []
