"""Concurrency-safe JSONL append helper.

PR-0 (audit CRITICAL #1): the codebase has 14 raw ``open(path, "a")``
JSONL appenders. On Linux, writes above ``PIPE_BUF`` (4 KiB) are NOT
atomic, so two concurrent appenders can interleave bytes mid-record
and silently corrupt the stream. ``_read_jsonl`` drops malformed
lines without warning, so the corruption is undiscoverable until an
external audit notices a missing record.

This module provides a single ``safe_append_jsonl`` that wraps the
append with the same ``InterProcessLock`` event_bus.py already uses
for its own appends. Plus an ``fsync`` so a power-cut between write
and OS flush doesn't silently truncate the last record.

Migration pattern: replace

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\\n")

with

    safe_append_jsonl(path, record)

The helper handles the dir creation, lock acquisition, JSON encoding,
trailing newline, and fsync for you. It does NOT validate ``record``
beyond "must JSON-encode" - callers retain responsibility for their
own schema.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Union

from .io import InterProcessLock

logger = logging.getLogger("nth_dao.util.jsonl_safe")

PathLike = Union[str, Path]


# ===== G-12 (Voss audit): lock-timeout tier constants =====
#
# A single hard-coded default makes callers pick arbitrary numbers.
# Three tiers documents the contention / criticality intent at the
# callsite, and lets a deployment retune all FAST callers (or all
# PATIENT callers) in one place without grepping for magic numbers.
#
# FAST (1 s):
#   high-frequency, low-stakes appenders. False-positive timeout is
#   preferable to long waits. Use for event-bus-style append loops.
#
# DEFAULT (5 s):
#   the original default. Balanced for moderate-contention callers
#   like group / channel audit logs.
#
# PATIENT (30 s):
#   low-frequency callers where a missed write is much worse than a
#   long wait - reputation endorsements, credit ledger, anything
#   feeding the "audit by default" promise. Set high enough to
#   absorb transient contention without surfacing a write failure.

LOCK_TIMEOUT_FAST: float = 1.0
LOCK_TIMEOUT_DEFAULT: float = 5.0
LOCK_TIMEOUT_PATIENT: float = 30.0


def safe_append_jsonl(
    path: PathLike,
    record: Mapping[str, Any],
    *,
    fsync: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_DEFAULT,
    external_lock_held: bool = False,
) -> None:
    """Append ``record`` as a single JSONL line, holding a file lock.

    Parameters
    ----------
    path
        Destination JSONL file. Parent directories are created if
        missing.
    record
        Anything ``json.dumps`` accepts. Must serialize without
        producing embedded newlines (Python's json never does this
        by default).
    fsync
        Default True. Forces the kernel buffer to disk before
        returning so a crash between append and flush cannot
        silently lose the record. Set False for hot-path callers
        that batch many appends behind a single fsync.
    lock_timeout
        Seconds to wait for the lock. Defaults to
        ``LOCK_TIMEOUT_DEFAULT`` (5 s). For audit-critical low-
        frequency callers (credit ledger, reputation endorsements)
        pass ``LOCK_TIMEOUT_PATIENT`` so transient contention does
        not surface as a missed write. For hot-loop appenders use
        ``LOCK_TIMEOUT_FAST``. Long-tail contention beyond the
        timeout surfaces as a ``TimeoutError`` from InterProcessLock
        instead of corruption.
    external_lock_held
        G-6 (Voss audit): set True when the caller is already
        holding a wider lock that covers this append (e.g. the
        marketplace credit lock). Skipping the internal
        InterProcessLock prevents nested-lock deadlocks AND keeps
        the append inside the caller's transaction boundary so a
        crash mid-call cannot leave the credit file updated but the
        ledger un-appended.

    Raises
    ------
    TypeError
        If ``record`` is not JSON serialisable.
    TimeoutError
        If ``lock_timeout`` elapses before the lock is acquired.
    OSError
        If the underlying write / fsync fails.
    """
    payload = json.dumps(record, ensure_ascii=False)
    if "\n" in payload:
        # json.dumps never produces a literal newline by default but
        # a custom default= callable could. Refuse to corrupt the
        # stream silently.
        raise ValueError(
            "JSON payload contains a literal newline; refusing to append "
            "as a single JSONL line"
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # G-5 (Voss audit): keep lock files in a hidden ``.locks``
    # subdirectory rather than sprawling them next to data files.
    # Operations sees a clean listing; backup tools can exclude
    # ``.locks/`` with one glob; one process's stale lock from a
    # crash is visible in one place. NOTE: InterProcessLock appends
    # ``.lock`` to the path we hand it, so we pass the BASE name
    # (without our own .lock suffix).
    def _do_write():
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload + "\n")
            if fsync:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as exc:
                    # On some filesystems (tmpfs / overlayfs in CI)
                    # fsync isn't fully supported. Don't pretend the
                    # call succeeded; surface to the caller log.
                    logger.warning(
                        "fsync failed for %s: %s (data is in OS cache "
                        "but may not survive a crash)", path, exc,
                    )

    if external_lock_held:
        # Caller is already holding a wider transaction lock; skip
        # taking our own to avoid nested deadlocks and to keep the
        # append inside the caller's atomic boundary.
        _do_write()
    else:
        lock_dir = path.parent / ".locks"
        lock_dir.mkdir(exist_ok=True)
        lock_path = lock_dir / path.name
        # InterProcessLock uses the same lock-file pattern event_bus.py
        # already uses for its own appends; using it here means we're
        # consistent with the safe primitive already trusted in
        # production.
        with InterProcessLock(lock_path, timeout=lock_timeout):
            _do_write()


def safe_append_jsonl_batch(
    path: PathLike,
    records: Iterable[Mapping[str, Any]],
    *,
    fsync: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_DEFAULT,
    external_lock_held: bool = False,
) -> int:
    """G-13 (Voss audit): batched append of many records under ONE lock.

    Equivalent in outcome to calling ``safe_append_jsonl`` N times,
    but acquires the lock once, opens the file once, and fsyncs once.
    For hot-path callers that flush many records together (e.g.
    event-bus drains, transcript dumps), this is materially faster
    AND has stronger semantics: the batch lands as a single locked
    transaction, so a concurrent reader holding the lock will either
    see all N records or none of them — never a partial batch.

    Validation strategy
    -------------------
    ALL records are JSON-encoded into a single payload BEFORE the
    file is opened. If any record fails to serialise (or contains an
    embedded newline), the whole batch is rejected and nothing is
    written. This is the audit-by-default posture - half-applied
    batches are the worst case for a downstream replayer.

    Parameters
    ----------
    path
        Destination JSONL file. Parent directories are created if
        missing.
    records
        Iterable of JSON-serialisable mappings. Empty iterables are a
        no-op (returns 0).
    fsync
        Default True. Single fsync at the end of the batch.
    lock_timeout
        Same semantics as ``safe_append_jsonl``. Long-running batch
        writers should consider ``LOCK_TIMEOUT_PATIENT``.
    external_lock_held
        Same semantics as ``safe_append_jsonl``: skip taking the
        internal lock when the caller is already inside a wider
        transaction.

    Returns
    -------
    int
        Number of records appended.

    Raises
    ------
    ValueError
        If any record contains a literal newline, or if any record
        fails to JSON-encode. The file is NOT touched in this case.
    TimeoutError
        If ``lock_timeout`` elapses before the lock is acquired.
    OSError
        If the underlying write / fsync fails.
    """
    # G-13: validate-all-before-write. We materialize the iterable into
    # a list of (payload-str-per-line) up front so a mid-batch encode
    # failure can't leave us with a half-written file.
    payloads: List[str] = []
    for i, record in enumerate(records):
        payload = json.dumps(record, ensure_ascii=False)
        if "\n" in payload:
            raise ValueError(
                f"record at index {i} contains a literal newline; "
                f"refusing to corrupt the JSONL stream"
            )
        payloads.append(payload)

    if not payloads:
        # Empty batch is a no-op. Do NOT acquire the lock or touch
        # the file - callers that conditionally batch shouldn't pay
        # for lock contention on empty drains.
        return 0

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _do_write():
        with open(path, "a", encoding="utf-8") as f:
            # Single write call when possible - the OS will atomic-ish
            # this for sub-PIPE_BUF writes, and even for larger ones
            # the lock guarantees no interleaving with other writers.
            f.write("\n".join(payloads) + "\n")
            if fsync:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as exc:
                    logger.warning(
                        "fsync failed for %s: %s (data is in OS cache "
                        "but may not survive a crash)", path, exc,
                    )

    if external_lock_held:
        _do_write()
    else:
        lock_dir = path.parent / ".locks"
        lock_dir.mkdir(exist_ok=True)
        lock_path = lock_dir / path.name
        with InterProcessLock(lock_path, timeout=lock_timeout):
            _do_write()

    return len(payloads)


__all__ = ["safe_append_jsonl", "safe_append_jsonl_batch"]
