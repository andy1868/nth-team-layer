"""DID persistence across process restart (2026-06-08).

The user-facing scenario this exists to defend:

    1. Bob clones NTH DAO -> first boot generates Bob's identity
    2. Alice sends Bob her did:key over Signal / pasted in the dashboard
    3. Bob clicks +Add by DID -> ``/api/agents/add(target_did=did:key:zAlice)``
    4. Bob's dashboard shows Alice in Recently Added / search
    5. ===  Bob restarts NTH DAO (new process)  ===
    6. Bob searches "alice" -> the home row STILL carries Alice's DID
       and Bob can still address her via that DID

Pre-fix the chain broke at step 5/6: the membership.json had Alice's
agent_id but no DID, and the search home enrichment only knew its own
``node_identity.did`` for the bootstrap admin row. Alice's row came
back with ``did=""`` and Bob could no longer reach her by DID.

This file uses the same workspace dir across two TestClient instances
to simulate a process restart on the SAME disk state. If any step
fails after the restart, the collaboration story is broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.identity import crypto_available
from nth_dao.web import create_app


pytestmark = pytest.mark.skipif(
    not crypto_available(),
    reason="DID persistence test requires PyNaCl",
)


_ALICE_DID = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"


@pytest.fixture
def bob_first_boot(tmp_path, monkeypatch) -> TestClient:
    """Bob boots NTH DAO for the first time. mDNS off, console auth off."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    return TestClient(create_app(tmp_path, require_console_auth=False))


# ===== add then immediately search (sanity) =====


def test_add_alice_by_did_then_search_returns_did(bob_first_boot, tmp_path):
    """Within ONE process, adding Alice by DID and then searching for
    her must surface that DID on the home row. This is the trivial
    case the persistence test below builds on."""
    add_resp = bob_first_boot.post(
        "/api/agents/add",
        json={
            "actor_id": "admin",
            "target_did": _ALICE_DID,
            "label": "Alice from Acme",
        },
    )
    assert add_resp.status_code == 200, add_resp.text
    alice_agent_id = add_resp.json()["agent_id"]

    search_resp = bob_first_boot.get(
        "/api/agents/search",
        params={"q": alice_agent_id, "actor_id": "admin"},
    )
    assert search_resp.status_code == 200
    home_rows = [
        r for r in search_resp.json()["results"]
        if r["source"] == "home" and r["agent_id"] == alice_agent_id
    ]
    assert len(home_rows) == 1
    assert home_rows[0]["did"] == _ALICE_DID


# ===== THE key collaboration test =====


def test_added_did_survives_process_restart(tmp_path, monkeypatch):
    """The headline invariant: Bob adds Alice by DID, restarts NTH
    DAO, searches for Alice - her DID is still there.

    "Restart" is simulated by tearing down the first TestClient (which
    drops the in-memory WebState) and constructing a SECOND TestClient
    over the SAME workspace directory. If the persistence is correct,
    the second client reads the contact book from disk and surfaces
    the same DID.
    """
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")

    # ── First boot: add Alice by DID ──────────────────────────────────
    bob_v1 = TestClient(create_app(tmp_path, require_console_auth=False))
    add = bob_v1.post(
        "/api/agents/add",
        json={
            "actor_id": "admin",
            "target_did": _ALICE_DID,
            "label": "Alice Wu",
        },
    )
    assert add.status_code == 200, add.text
    alice_id = add.json()["agent_id"]

    # Tear down to drop in-memory state. The TestClient context-manager
    # exit closes the app; we discard the reference too.
    bob_v1.close()
    del bob_v1

    # ── Restart: new TestClient over the SAME workspace ───────────────
    bob_v2 = TestClient(create_app(tmp_path, require_console_auth=False))
    search = bob_v2.get(
        "/api/agents/search",
        params={"q": alice_id, "actor_id": "admin"},
    )
    assert search.status_code == 200
    home_alice = [
        r for r in search.json()["results"]
        if r["source"] == "home" and r["agent_id"] == alice_id
    ]
    assert len(home_alice) == 1, (
        f"Alice's home row vanished after restart; results: "
        f"{search.json()['results']}"
    )
    assert home_alice[0]["did"] == _ALICE_DID, (
        f"Alice's DID was lost across restart. The contact book did "
        f"NOT persist target_did from /api/agents/add. "
        f"Row: {home_alice[0]}"
    )
    # Label also carries (sparse-merge from the original add)
    assert home_alice[0].get("label", "") in ("Alice Wu", "")


def test_add_by_did_persists_pubkey_prefix(tmp_path, monkeypatch):
    """The 16-hex pubkey_prefix is the search row's redacted identity
    marker. It must survive restart for the same reason DID must."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    bob_v1 = TestClient(create_app(tmp_path, require_console_auth=False))
    add = bob_v1.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_did": _ALICE_DID},
    )
    alice_id = add.json()["agent_id"]
    bob_v1.close()

    bob_v2 = TestClient(create_app(tmp_path, require_console_auth=False))
    search = bob_v2.get(
        "/api/agents/search",
        params={"q": alice_id, "actor_id": "admin"},
    )
    row = [
        r for r in search.json()["results"]
        if r["source"] == "home" and r["agent_id"] == alice_id
    ][0]
    assert row["pubkey_prefix"], (
        f"pubkey_prefix was lost across restart; row={row}"
    )
    assert len(row["pubkey_prefix"]) == 16


def test_admin_caller_sees_full_pubkey_hex_after_restart(tmp_path, monkeypatch):
    """C-1 redaction rules still apply post-restart: admin caller sees
    the full pubkey_hex from the contact book; non-admin sees only the
    prefix."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    bob_v1 = TestClient(create_app(tmp_path, require_console_auth=False))
    add = bob_v1.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_did": _ALICE_DID},
    )
    alice_id = add.json()["agent_id"]
    bob_v1.close()

    bob_v2 = TestClient(create_app(tmp_path, require_console_auth=False))
    # admin caller -> sees full pubkey
    search_admin = bob_v2.get(
        "/api/agents/search",
        params={"q": alice_id, "actor_id": "admin"},
    )
    admin_row = [
        r for r in search_admin.json()["results"]
        if r["source"] == "home" and r["agent_id"] == alice_id
    ][0]
    assert len(admin_row.get("pubkey_hex", "")) == 64

    # non-admin: promote alice to plain member so she can search; her
    # row about herself should still NOT carry pubkey_hex
    join = bob_v2.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "carol"},
    )
    assert join.status_code == 200
    search_carol = bob_v2.get(
        "/api/agents/search",
        params={"q": alice_id, "actor_id": "carol"},
    )
    carol_row = [
        r for r in search_carol.json()["results"]
        if r["source"] == "home" and r["agent_id"] == alice_id
    ][0]
    assert "pubkey_hex" not in carol_row or carol_row.get("pubkey_hex") == ""
    # but did + prefix are still visible
    assert carol_row["did"] == _ALICE_DID
    assert len(carol_row["pubkey_prefix"]) == 16


# ===== ContactBook file is on disk where we expect =====


def test_contact_book_file_appears_at_expected_path(tmp_path, monkeypatch):
    """Document the on-disk path so an operator knows where to look
    when debugging."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    client = TestClient(create_app(tmp_path, require_console_auth=False))
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_did": _ALICE_DID},
    )
    contacts_path = tmp_path / "team_contacts" / "contacts.jsonl"
    assert contacts_path.exists()
    assert contacts_path.read_text(encoding="utf-8").count(_ALICE_DID) >= 1


def test_add_by_did_reports_contact_book_write_failure(
    tmp_path, monkeypatch,
):
    """For a DID add, persisting the DID is the core operation. If the
    contact book write fails, the route must not return a clean 200 that
    makes the UI believe the peer is addressable after restart."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    client = TestClient(create_app(tmp_path, require_console_auth=False))

    def fail_add(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(client.app.state.nth.contacts, "add", fail_add)
    resp = client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_did": _ALICE_DID},
    )
    assert resp.status_code == 500
    assert "DID contact persistence failed" in resp.json()["detail"]


# ===== adding by agent_id only (no DID) does NOT inject fake DID =====


def test_add_by_agent_id_only_leaves_did_blank_after_restart(
    tmp_path, monkeypatch,
):
    """If the caller only supplies target_agent_id (no DID), the
    contact book records that fact - the next search MUST NOT make up
    a DID. Otherwise non-DID adds would silently masquerade as DID
    adds and downstream verification breaks."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    bob_v1 = TestClient(create_app(tmp_path, require_console_auth=False))
    bob_v1.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "dave"},
    )
    bob_v1.close()

    bob_v2 = TestClient(create_app(tmp_path, require_console_auth=False))
    search = bob_v2.get(
        "/api/agents/search",
        params={"q": "dave", "actor_id": "admin"},
    )
    dave_row = [
        r for r in search.json()["results"]
        if r["source"] == "home" and r["agent_id"] == "dave"
    ][0]
    assert dave_row["did"] == ""
    assert dave_row["pubkey_prefix"] == ""
