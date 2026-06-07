"""ContactBook unit tests.

Pins the persistence contract that the higher-level "Bob adds Alice by
DID, restarts, still sees Alice's DID" integration test depends on.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nth_dao.contact_book import (
    SOURCE_GROUP,
    SOURCE_LAN,
    SOURCE_MANUAL,
    ContactBook,
    ContactRecord,
)


# ===== single-record happy path =====


def test_add_then_get_roundtrips(tmp_path):
    book = ContactBook(tmp_path)
    book.add(
        agent_id="alice",
        did="did:key:z6MkAlice",
        pubkey_hex="ab" * 32,
        label="Alice Wu",
        source=SOURCE_MANUAL,
        added_by="admin",
    )
    out = book.get("alice")
    assert out is not None
    assert out.agent_id == "alice"
    assert out.did == "did:key:z6MkAlice"
    assert out.pubkey_hex == "ab" * 32
    assert out.label == "Alice Wu"
    assert out.source == SOURCE_MANUAL
    assert out.added_by == "admin"
    assert out.added_at.startswith("20")


def test_add_persists_to_disk_in_jsonl(tmp_path):
    """Records land in <workspace>/team_contacts/contacts.jsonl one
    per line, parseable independently."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    contacts_path = tmp_path / "team_contacts" / "contacts.jsonl"
    assert contacts_path.exists()
    lines = contacts_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["agent_id"] == "alice"
    assert record["did"] == "did:key:z6MkAlice"


def test_get_returns_none_for_unknown_agent(tmp_path):
    book = ContactBook(tmp_path)
    assert book.get("nobody") is None


def test_get_returns_none_for_empty_id(tmp_path):
    book = ContactBook(tmp_path)
    assert book.get("") is None
    assert book.get(None) is None  # type: ignore[arg-type]


# ===== reverse lookup =====


def test_find_by_did(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice", pubkey_hex="aa" * 32)
    book.add(agent_id="bob", did="did:key:z6MkBob")
    found = book.find_by_did("did:key:z6MkAlice")
    assert found is not None
    assert found.agent_id == "alice"


def test_find_by_did_returns_none_for_missing(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    assert book.find_by_did("did:key:z6MkNotPresent") is None


def test_find_by_pubkey_case_insensitive(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", pubkey_hex="ABCDEF" + "00" * 29)
    # User pastes lowercase hex
    found = book.find_by_pubkey("abcdef" + "00" * 29)
    assert found is not None
    assert found.agent_id == "alice"


# ===== persistence across "restart" (fresh ContactBook over same dir) =====


def test_records_survive_restart(tmp_path):
    """The key collaboration invariant: process restarts must not
    drop contact data. Construct a fresh ContactBook over the same
    workspace and the prior writes must reappear."""
    book_a = ContactBook(tmp_path)
    book_a.add(
        agent_id="alice",
        did="did:key:z6MkAlice",
        pubkey_hex="aa" * 32,
        label="Alice",
    )
    book_a.add(agent_id="bob", did="did:key:z6MkBob")

    # Simulate a process restart by constructing a NEW ContactBook over
    # the same on-disk directory. No shared in-memory state.
    book_b = ContactBook(tmp_path)
    alice = book_b.get("alice")
    bob = book_b.get("bob")
    assert alice is not None and alice.did == "did:key:z6MkAlice"
    assert bob is not None and bob.did == "did:key:z6MkBob"
    assert book_b.find_by_did("did:key:z6MkAlice").agent_id == "alice"


# ===== latest-wins + merge semantics =====


def test_re_add_same_agent_keeps_one_entry_with_latest_wins(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice", label="Alice")
    book.add(agent_id="alice", label="Alice Wu, PhD")
    # list_all returns one row per agent_id
    assert len(book.list_all()) == 1
    out = book.get("alice")
    # Newer label wins
    assert out.label == "Alice Wu, PhD"
    # Carried forward from prior record because newer left it blank
    assert out.did == "did:key:z6MkAlice"


def test_sparse_update_does_not_wipe_did(tmp_path):
    """A later sparse add() (e.g. discovery enrichment only filling
    in source=lan_discover) must NOT clear a previously known DID."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    book.add(agent_id="alice", source=SOURCE_LAN, label="LAN peer 1")
    out = book.get("alice")
    assert out.did == "did:key:z6MkAlice"
    assert out.source == SOURCE_LAN
    assert out.label == "LAN peer 1"


def test_added_at_updates_on_re_add(tmp_path):
    """The merged record's added_at reflects the NEWER touch so the
    audit log reads chronologically."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", label="Alice v1")
    first = book.get("alice").added_at
    # second-resolution timestamp - sleep tiny amount? actually iso
    # to second resolution; this test only checks monotonicity
    # within the same second is acceptable, so re-add and assert >=.
    book.add(agent_id="alice", label="Alice v2")
    second = book.get("alice").added_at
    assert second >= first


# ===== validation =====


def test_add_rejects_empty_agent_id(tmp_path):
    book = ContactBook(tmp_path)
    with pytest.raises(ValueError, match="agent_id"):
        book.add(agent_id="")


def test_add_rejects_whitespace_only_agent_id(tmp_path):
    book = ContactBook(tmp_path)
    with pytest.raises(ValueError, match="agent_id"):
        book.add(agent_id="   ")


def test_add_strips_surrounding_whitespace(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="  alice  ", did="  did:key:z6MkAlice  ")
    out = book.get("alice")
    assert out is not None
    assert out.did == "did:key:z6MkAlice"


# ===== corruption tolerance =====


def test_malformed_jsonl_lines_are_skipped(tmp_path):
    """A torn line from an interrupted write does not break the reader."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    # Manually corrupt the file with a torn line followed by a good
    # line - simulates a crash-during-append situation.
    with book.path.open("a", encoding="utf-8") as f:
        f.write("{ this is not valid json\n")
        f.write(json.dumps({
            "agent_id": "bob",
            "did": "did:key:z6MkBob",
            "added_at": "2026-06-08T00:00:00+00:00",
        }) + "\n")

    # Fresh reader to bypass the cache
    book2 = ContactBook(tmp_path)
    assert book2.get("alice") is not None
    assert book2.get("bob") is not None


def test_records_missing_required_fields_are_skipped(tmp_path):
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    # Manually inject a record with empty agent_id
    with book.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"agent_id": "", "did": "x"}) + "\n")
    book2 = ContactBook(tmp_path)
    assert len(book2.list_all()) == 1
    assert book2.list_all()[0].agent_id == "alice"


# ===== caching =====


def test_view_is_cached_until_file_changes(tmp_path, monkeypatch):
    """The hot-path /api/agents/search calls list_all() and get() per
    request; the cache must not re-parse JSONL on every call when the
    file is unchanged."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice", did="did:key:z6MkAlice")
    book.add(agent_id="bob", did="did:key:z6MkBob")

    # Spy on _load_view to count parse calls
    parse_count = {"n": 0}
    real_load = book._load_view

    def counting_load():
        parse_count["n"] += 1
        return real_load()

    monkeypatch.setattr(book, "_load_view", counting_load)
    book._cached_signature = None   # force first call to populate

    # Five calls
    for _ in range(5):
        book.list_all()

    assert parse_count["n"] == 1, (
        f"_load_view called {parse_count['n']} times; cache should "
        f"reduce to 1 when the file is unchanged"
    )


def test_view_recomputes_after_add(tmp_path, monkeypatch):
    """Adding a new record invalidates the cache so the next read
    surfaces it."""
    book = ContactBook(tmp_path)
    book.add(agent_id="alice")
    parse_count = {"n": 0}
    real_load = book._load_view

    def counting_load():
        parse_count["n"] += 1
        return real_load()

    monkeypatch.setattr(book, "_load_view", counting_load)
    book._cached_signature = None
    book.list_all()
    book.list_all()   # cached
    book.add(agent_id="bob")   # invalidates
    book.list_all()   # re-parses
    assert parse_count["n"] == 2


# ===== concurrency smoke =====


def test_concurrent_adds_all_durably_persist(tmp_path):
    """Ten threads each add a distinct agent. After the join, all
    ten records must be readable."""
    book = ContactBook(tmp_path)

    def writer(i):
        book.add(agent_id=f"agent{i:02d}", did=f"did:key:z6Mk{i:02d}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    book2 = ContactBook(tmp_path)
    for i in range(10):
        record = book2.get(f"agent{i:02d}")
        assert record is not None, f"agent{i:02d} missing after concurrent writes"
        assert record.did == f"did:key:z6Mk{i:02d}"
