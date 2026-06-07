"""Architect R-13 (2026-06-07): /api/agents/by_code/{code} requires
actor_id + redacts pubkey for non-admins.

Pre-fix the endpoint was a smaller cousin of /api/agents/search -
returning a group member's full ``pubkey_hex`` to any unauthenticated
caller who guessed a valid code. The frontend wrapper that consumed
this endpoint has been removed (dead since Week-1 Task 2); the
endpoint itself remains available for external (non-dashboard)
callers, now authentic and redacted.

Pins:
  * 400 without actor_id
  * 403 for non-member actor
  * admin gets full pubkey_hex
  * non-admin member gets pubkey_prefix + empty pubkey_hex
  * 404 still works for unknown codes
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def _seed_group_with(tmp_path: Path, pk: str) -> str:
    from nth_dao.group_registry import GroupPolicy, GroupRegistry
    registry = GroupRegistry(tmp_path)
    path = registry._path_for_slug("qa-team")
    path.write_text(json.dumps({
        "group_id": "grp-qa",
        "slug": "qa-team",
        "display_name": "QA Team",
        "description": "",
        "policy": GroupPolicy.OPEN.value,
        "founder_pubkey": pk,
        "member_pubkeys": [pk],
        "admin_pubkeys": [pk],
        "signer_pubkey": pk,
        "sig": "fake",
        "created_at": "2026-06-07T00:00:00",
        "updated_at": "2026-06-07T00:00:00",
        "metadata": {},
    }), encoding="utf-8")
    from nth_dao.agent_code import code_for_pubkey
    return code_for_pubkey(pk)


# ===== auth gate =====


def test_R13_by_code_requires_actor_id(client):
    resp = client.get("/api/agents/by_code/aaaa-bbbb")
    assert resp.status_code == 400
    assert "actor_id" in resp.json()["detail"]


def test_R13_by_code_rejects_non_member(client):
    resp = client.get(
        "/api/agents/by_code/aaaa-bbbb",
        params={"actor_id": "stranger"},
    )
    assert resp.status_code == 403


# ===== pubkey redaction parity with /api/agents/search C-1 =====


def test_R13_admin_gets_full_pubkey_for_group_match(client, tmp_path):
    pk = "ab" * 32
    code = _seed_group_with(tmp_path, pk)
    resp = client.get(
        f"/api/agents/by_code/{code}",
        params={"actor_id": "admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "group"
    assert body["pubkey_hex"] == pk
    assert body["pubkey_prefix"] == pk[:16]


def test_R13_non_admin_member_gets_redacted_pubkey(client, tmp_path):
    pk = "cd" * 32
    code = _seed_group_with(tmp_path, pk)
    # Promote 'alice' to plain member (not admin)
    client.post("/api/agents/add", json={
        "actor_id": "admin", "target_agent_id": "alice",
    })
    resp = client.get(
        f"/api/agents/by_code/{code}",
        params={"actor_id": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Field is present (shape stable) but value masked
    assert body["pubkey_hex"] == ""
    assert body["pubkey_prefix"] == pk[:16]


# ===== home source unchanged =====


def test_R13_home_member_lookup_does_not_leak_pubkey(client):
    """R-35 (2026-06-08): the bootstrap admin's code is now derived
    from the node's pubkey, not the literal "admin" string. We pull
    the actual code from /api/identity rather than re-computing the
    legacy constant.

    Pre-fix this test pinned ``pubkey_hex == ""`` for home members,
    which masked R-1: the by_code endpoint returned no pubkey at all.
    Now the bootstrap admin's lookup CAN expose a pubkey (it's this
    node's own pubkey - public information). We honour the C-1
    redaction posture: admin caller sees full pubkey_hex, non-admin
    members see only ``pubkey_prefix``.
    """
    code = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    resp = client.get(
        f"/api/agents/by_code/{code}",
        params={"actor_id": "admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "home"
    # admin caller -> full pubkey
    assert len(body.get("pubkey_hex", "")) == 64
    # prefix always present for the operator's quick visual check
    assert len(body.get("pubkey_prefix", "")) == 16


# ===== 404 path still works =====


def test_R13_unknown_code_still_404_with_auth(client):
    resp = client.get(
        "/api/agents/by_code/ffff-0000",
        params={"actor_id": "admin"},
    )
    assert resp.status_code == 404
