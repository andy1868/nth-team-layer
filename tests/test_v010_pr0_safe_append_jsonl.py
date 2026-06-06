"""PR-0: safe_append_jsonl concurrency safety + crash semantics.

The audit (NTH_DAO_AUDIT.md CRITICAL #1) flagged 14 raw
``open(path, "a")`` JSONL appenders in production. Concurrent writes
above PIPE_BUF (4 KiB on Linux) interleave silently, and
``_read_jsonl`` drops malformed lines without warning - corruption
goes undetected until an external audit notices the missing data.

This module pins the contract of ``safe_append_jsonl``:

  * Concurrent writers across THREADS produce no interleaving
  * Concurrent writers across PROCESSES produce no interleaving
  * Records that JSON-encode to >4 KiB still don't tear (the
    PIPE_BUF threshold the audit specifically mentioned)
  * Missing parent directories are created
  * fsync=True is the default

These tests STRESS the implementation - if any of them flake, the
race condition is real and the helper is not yet safe.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import threading
from pathlib import Path

import pytest

from nth_dao.util import safe_append_jsonl


def _read_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file strictly; raise on any malformed line so
    interleaved writes fail loudly in tests."""
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"corrupt JSONL line {line_no}: {exc} (line={line!r})"
                ) from exc
    return out


# =====================================================================
# Basic contract
# =====================================================================


def test_PR0_basic_append_writes_one_line_per_record(tmp_path):
    path = tmp_path / "events.jsonl"
    safe_append_jsonl(path, {"i": 1})
    safe_append_jsonl(path, {"i": 2})
    safe_append_jsonl(path, {"i": 3})
    records = _read_jsonl(path)
    assert records == [{"i": 1}, {"i": 2}, {"i": 3}]


def test_PR0_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "deep" / "nested" / "events.jsonl"
    safe_append_jsonl(path, {"created": True})
    assert path.exists()
    assert _read_jsonl(path) == [{"created": True}]


def test_PR0_embedded_newline_in_string_is_escaped(tmp_path):
    """json.dumps escapes \\n to \\\\n inside string values so the
    JSONL invariant (one record per line) is preserved automatically."""
    path = tmp_path / "newlines.jsonl"
    safe_append_jsonl(path, {"msg": "line1\nline2"})
    records = _read_jsonl(path)
    assert records == [{"msg": "line1\nline2"}]


def test_PR0_rejects_non_json_serialisable(tmp_path):
    path = tmp_path / "bad.jsonl"
    with pytest.raises(TypeError):
        safe_append_jsonl(path, {"obj": object()})


# =====================================================================
# Thread concurrency (single-process race the audit highlights)
# =====================================================================


def test_PR0_concurrent_threads_no_interleaving(tmp_path):
    """10 threads x 100 appends each = 1000 records. The strict
    JSONL parser must see all 1000 as well-formed."""
    path = tmp_path / "concurrent.jsonl"
    N_THREADS = 10
    PER_THREAD = 100

    def writer(thread_id: int) -> None:
        for i in range(PER_THREAD):
            safe_append_jsonl(path, {"t": thread_id, "i": i})

    threads = [
        threading.Thread(target=writer, args=(t,))
        for t in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = _read_jsonl(path)
    assert len(records) == N_THREADS * PER_THREAD, (
        f"lost records under concurrent writes: expected "
        f"{N_THREADS * PER_THREAD}, got {len(records)}"
    )
    # All thread/index pairs should be unique - no duplicates from
    # interleaving manifesting as parseable-but-merged records.
    pairs = {(r["t"], r["i"]) for r in records}
    assert len(pairs) == N_THREADS * PER_THREAD


def test_PR0_large_records_above_pipe_buf_no_tearing(tmp_path):
    """The audit specifically mentioned ``> PIPE_BUF`` as the trigger.
    PIPE_BUF is 4 KiB on Linux; we use 8 KiB records to be safely
    over the threshold."""
    path = tmp_path / "large.jsonl"
    big_payload = "X" * 8000
    N_THREADS = 8
    PER_THREAD = 50

    def writer(thread_id: int) -> None:
        for i in range(PER_THREAD):
            safe_append_jsonl(path, {
                "t": thread_id, "i": i, "data": big_payload,
            })

    threads = [
        threading.Thread(target=writer, args=(t,))
        for t in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = _read_jsonl(path)
    assert len(records) == N_THREADS * PER_THREAD
    # Every big_payload must round-trip intact (no truncation /
    # mid-record splice).
    for r in records:
        assert r["data"] == big_payload, "payload corrupted - cross-write tear"


# =====================================================================
# Process concurrency (the real cross-process race)
# =====================================================================


def _process_writer(path_str: str, proc_id: int, count: int) -> None:
    """Module-level so multiprocessing's spawn can import it."""
    from nth_dao.util import safe_append_jsonl as _sa
    for i in range(count):
        _sa(Path(path_str), {"p": proc_id, "i": i})


def test_PR0_concurrent_processes_no_interleaving(tmp_path):
    """The real audit concern: TWO PROCESSES appending to the same
    JSONL. InterProcessLock should serialize at OS level."""
    if os.name == "nt":
        pytest.skip("Windows multiprocessing has fork limitations; "
                    "thread-level test covers the same logic surface")
    path = tmp_path / "cross_process.jsonl"
    N_PROCS = 4
    PER_PROC = 50

    procs = [
        mp.Process(target=_process_writer, args=(str(path), p, PER_PROC))
        for p in range(N_PROCS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, f"writer process failed: {p.exitcode}"

    records = _read_jsonl(path)
    assert len(records) == N_PROCS * PER_PROC


# =====================================================================
# Crash semantics: fsync default keeps records on disk
# =====================================================================


def test_PR0_default_fsyncs(tmp_path):
    """Default ``fsync=True`` flushes to disk. We can't easily test
    "survives a kill -9" in a unit test, but we CAN test that after
    the call returns the file is visible to a fresh open."""
    path = tmp_path / "fsync.jsonl"
    safe_append_jsonl(path, {"x": 1})
    assert path.stat().st_size > 0
    assert _read_jsonl(path) == [{"x": 1}]


def test_PR0_fsync_off_is_an_explicit_choice(tmp_path):
    """``fsync=False`` is for hot loops that batch their own flush.
    Records are still appended; the only difference is the durability
    guarantee."""
    path = tmp_path / "no_fsync.jsonl"
    for i in range(5):
        safe_append_jsonl(path, {"i": i}, fsync=False)
    records = _read_jsonl(path)
    assert records == [{"i": i} for i in range(5)]
