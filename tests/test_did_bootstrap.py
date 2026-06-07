"""DID bootstrap (2026-06-07): each fresh install auto-generates a
unique Ed25519 identity that becomes the workspace's permanent DID.

User-facing scenario this enables:
    Alice clones the repo -> first ``python -m nth_dao.web``
    -> server writes ``<workspace>/identity/identity.json`` (mode 0600)
    -> ``did:key:z6Mk...`` is now Alice's permanent address
    -> Bob asks Alice for her DID, pastes it into "Add by DID"
    -> Bob's NTH DAO resolves Alice's identity

Pre-fix the bootstrap created an ``admin`` member without any key
material, so every install was "anonymous" and friend-finding had
nothing to look up except an opaque "admin" string.

Pins:
  * fresh workspace -> identity.json created with valid did:key
  * two fresh workspaces -> two DIFFERENT did:key values
  * second boot of the SAME workspace -> SAME did:key (stable identity)
  * team.json's owner_pubkey is filled in from the identity
  * /api/identity returns the DID + pubkey + code
  * /api/state's actor block includes did when actor is admin
  * /api/agents/search admin row exposes did + pubkey_prefix
  * non-admin caller sees pubkey_prefix (16 hex) but NOT pubkey_hex
  * graceful degradation when pynacl/identity is unavailable
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.identity import crypto_available, default_identity_path
from nth_dao.web import create_app


pytestmark = pytest.mark.skipif(
    not crypto_available(),
    reason="DID bootstrap requires PyNaCl",
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


# ===== identity.json materialises on first boot =====


def test_DID1_first_boot_creates_identity_json(tmp_path):
    """``_bootstrap`` must auto-generate the workspace identity.

    Note: ``identity.json`` stores the raw 32-byte pubkey hex, not the
    derived did:key string. The DID is computed at runtime via
    ``AgentIdentity.as_did()``. We verify the persisted file holds a
    valid Ed25519 pubkey and that loading it then produces a real DID.
    """
    from nth_dao.identity import AgentIdentity
    create_app(tmp_path)
    identity_path = default_identity_path(tmp_path)
    assert identity_path.exists(), (
        f"identity.json missing at {identity_path}; bootstrap did not "
        f"auto-generate the workspace identity"
    )
    ident = AgentIdentity.load(identity_path)
    did = ident.as_did()
    assert did.startswith("did:key:z"), (
        f"loaded identity did not produce a did:key: {did!r}"
    )
    assert len(ident.pubkey_hex) == 64


def test_DID2_two_workspaces_get_distinct_dids(tmp_path):
    """Fresh installs are NOT clones - the keys must be random."""
    ws_a = tmp_path / "alice"
    ws_b = tmp_path / "bob"
    ws_a.mkdir(); ws_b.mkdir()
    client_a = TestClient(create_app(ws_a))
    client_b = TestClient(create_app(ws_b))
    did_a = client_a.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["did"]
    did_b = client_b.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["did"]
    assert did_a and did_b
    assert did_a != did_b, "two fresh workspaces produced the SAME DID"


def test_DID3_reboot_keeps_same_did(tmp_path):
    """The identity persists; the second boot reads the same file."""
    create_app(tmp_path)
    first_text = default_identity_path(tmp_path).read_text(encoding="utf-8")
    # Re-create the app (simulates a process restart on the same workspace)
    create_app(tmp_path)
    second_text = default_identity_path(tmp_path).read_text(encoding="utf-8")
    assert first_text == second_text, (
        "identity.json changed across reboots - the workspace's DID "
        "must be stable for the lifetime of the install"
    )


# ===== team.json is signed by the bootstrap DID =====


def test_DID4_team_json_owner_pubkey_pinned_from_identity(tmp_path):
    create_app(tmp_path)
    import json
    team_path = tmp_path / "team.json"
    assert team_path.exists()
    cfg = json.loads(team_path.read_text(encoding="utf-8"))
    assert cfg.get("owner_pubkey"), (
        "team.json owner_pubkey is empty - bootstrap did not pin the "
        "node identity (unsigned team.json is a tampering risk)"
    )
    # And it matches the identity file's pubkey
    from nth_dao.identity import AgentIdentity
    ident = AgentIdentity.load(default_identity_path(tmp_path))
    assert cfg["owner_pubkey"] == ident.pubkey_hex


# ===== /api/identity endpoint =====


def test_DID5_identity_endpoint_requires_actor_id(client):
    resp = client.get("/api/identity")
    assert resp.status_code == 400
    assert "actor_id" in resp.json()["detail"]


def test_DID6_identity_endpoint_returns_full_shape_for_admin(client):
    resp = client.get("/api/identity", params={"actor_id": "admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "admin"
    assert body["did"].startswith("did:key:z")
    assert len(body["pubkey_hex"]) == 64    # 32-byte ed25519 pubkey hex
    assert body["pubkey_prefix"] == body["pubkey_hex"][:16]
    assert body["code"]
    assert body["bootstrap_error"] == ""


def test_DID7_identity_endpoint_rejects_non_member(client):
    resp = client.get("/api/identity", params={"actor_id": "stranger"})
    assert resp.status_code == 403


# ===== /api/state.actor includes DID =====


def test_DID8_state_actor_includes_did_for_admin(client):
    resp = client.get("/api/state", params={"agent_id": "admin"})
    assert resp.status_code == 200
    actor = resp.json()["actor"]
    assert actor["did"].startswith("did:key:z")
    assert len(actor["pubkey_hex"]) == 64


def test_DID9_state_actor_did_is_empty_for_non_admin_member(client):
    # alice is just a member, not the bootstrap admin - so the node's
    # DID is NOT her DID. She'd have her own elsewhere; this endpoint
    # answers only "what is the node's identity".
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "alice"},
    )
    resp = client.get("/api/state", params={"agent_id": "alice"})
    actor = resp.json()["actor"]
    assert actor["did"] == ""
    assert actor["pubkey_hex"] == ""


# ===== /api/agents/search exposes DID on the admin home row =====


def test_DID10_search_admin_row_has_did(client):
    resp = client.get(
        "/api/agents/search",
        params={"q": "admin", "actor_id": "admin"},
    )
    home_admin = [
        r for r in resp.json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    ]
    assert len(home_admin) == 1
    row = home_admin[0]
    assert row["did"].startswith("did:key:z")
    # admin caller -> full pubkey
    assert len(row.get("pubkey_hex", "")) == 64
    assert row["pubkey_prefix"] == row["pubkey_hex"][:16]


def test_DID11_search_admin_row_redacts_pubkey_for_non_admin(client):
    """Architect R-1 parity: even a member can see the DID + prefix,
    but the full pubkey_hex stays behind the admin gate."""
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "alice"},
    )
    resp = client.get(
        "/api/agents/search",
        params={"q": "admin", "actor_id": "alice"},
    )
    home_admin = [
        r for r in resp.json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    ]
    assert len(home_admin) == 1
    row = home_admin[0]
    # alice still sees the public DID (the whole point of having one)
    assert row["did"].startswith("did:key:z")
    # And the prefix (16 hex - usable for matching, not for forging)
    assert len(row["pubkey_prefix"]) == 16
    # But NOT the full pubkey
    assert "pubkey_hex" not in row or row.get("pubkey_hex") in ("", None)


# ===== non-admin members in home set have no DID exposed =====


def test_DID12_non_admin_member_home_row_has_empty_did(client):
    """Adding alice as a member does NOT magically give the dashboard
    her DID - she's a remote identity we don't have key material for."""
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "alice"},
    )
    resp = client.get(
        "/api/agents/search",
        params={"q": "alice", "actor_id": "admin"},
    )
    alice_row = [
        r for r in resp.json()["results"]
        if r["agent_id"] == "alice" and r["source"] == "home"
    ]
    assert len(alice_row) == 1
    assert alice_row[0]["did"] == ""
    assert alice_row[0]["pubkey_prefix"] == ""
