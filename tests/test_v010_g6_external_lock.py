"""G-6 (Voss audit): safe_append_jsonl accepts external_lock_held=True
so the caller's outer transaction lock covers the append.

The marketplace credit transfer was the canonical case: the
_transfer_credits flow holds a .credit.lock for the read-check-write
on credits.json, then calls _write_credits → safe_append_jsonl →
which used to take its OWN .jsonl.lock for the ledger append. Two
different locks for one atomic update.

If a crash happened between the credit-file write and the ledger
append, the credit balance was modified but the ledger had no
record - breaking the ledger's "reconstruct credits from history"
contract.

The fix: external_lock_held=True skips the internal lock so the
caller's lock covers everything.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nth_dao.util import safe_append_jsonl


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line:
                out.append(json.loads(line))
    return out


def test_G6_external_lock_held_skips_internal_lock(tmp_path):
    """When external_lock_held=True, no .locks/<name>.lock file is
    created (caller's outer lock should be the only one)."""
    path = tmp_path / "events.jsonl"
    safe_append_jsonl(path, {"i": 1}, external_lock_held=True)
    assert _read_jsonl(path) == [{"i": 1}]
    # No internal lock file
    assert not (tmp_path / ".locks").exists() or \
        not list((tmp_path / ".locks").glob("*"))


def test_G6_default_takes_internal_lock(tmp_path):
    """Sanity: default behaviour still takes the internal lock so
    callers without an outer transaction stay safe."""
    path = tmp_path / "events.jsonl"
    safe_append_jsonl(path, {"i": 1})
    assert _read_jsonl(path) == [{"i": 1}]
    assert (tmp_path / ".locks" / "events.jsonl.lock").exists()


def test_G6_external_lock_held_under_caller_lock_serialises_threads(tmp_path):
    """Simulate the marketplace pattern: caller takes a wider lock,
    then calls safe_append_jsonl(external_lock_held=True). The outer
    lock must still serialise concurrent threads correctly."""
    path = tmp_path / "ledger.jsonl"
    outer_lock = threading.Lock()
    N_THREADS = 10
    PER_THREAD = 30

    def worker(tid: int):
        for i in range(PER_THREAD):
            with outer_lock:
                safe_append_jsonl(
                    path, {"t": tid, "i": i}, external_lock_held=True,
                )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = _read_jsonl(path)
    assert len(records) == N_THREADS * PER_THREAD
    pairs = {(r["t"], r["i"]) for r in records}
    assert len(pairs) == N_THREADS * PER_THREAD


def test_G6_marketplace_transfer_credits_records_ledger_atomically(tmp_path):
    """End-to-end: marketplace._transfer_credits must update
    credits.json AND append to ledger in one transaction. After a
    successful credit, both files reflect the new state."""
    from nth_dao.marketplace import TaskMarketplace
    from nth_dao.identity import AgentIdentity

    identity = AgentIdentity.from_string("alice", label="alice")
    mp = TaskMarketplace(
        agent_id="alice",
        identity=identity,
        workspace=tmp_path,
    )
    # Start at 100, debit 30
    before = mp.balance
    mp._transfer_credits(delta=-30.0, order_id="ord-1", kind="test_debit")
    after = mp.balance
    assert abs(after - (before - 30.0)) < 0.01

    # Ledger has the entry
    ledger_path = tmp_path / "team_marketplace" / "alice_credits.ledger.jsonl"
    assert ledger_path.exists()
    entries = _read_jsonl(ledger_path)
    assert len(entries) >= 1
    last = entries[-1]
    assert last["kind"] == "test_debit"
    assert last["delta"] == -30.0
    assert last["order_id"] == "ord-1"
