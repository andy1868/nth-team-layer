"""Architect audit C-1 (2026-06-07): /api/agents/search requires auth +
redacts pubkey for non-admins.

Before the fix, GET /api/agents/search?q=<anything> returned:
  * full team-member roster with role (admin/member)
  * every group member's full Ed25519 pubkey_hex with role + group_slug

This let any unauthenticated caller enumerate the entire social graph.
Now:
  * actor_id is required (400 if missing)
  * non-members are rejected with 403 (same gate as the rest of the console)
  * non-admin callers see only ``pubkey_prefix`` (16 hex chars), not
    the full pubkey_hex
  * admins see the full pubkey_hex

Also pins:
  * H-4: dedup is keyed on (source, agent_id), so a 16-char pubkey
    prefix cannot collide with a real agent_id and silently drop one
  * M-1: ``?limit=foo`` returns a 400, not a 500
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


# ===== C-1 auth gate =====


def test_C1_search_without_actor_id_returns_400(client):
    """No actor_id -> 400. Was 200 with full enumeration before fix."""
    resp = client.get("/api/agents/search", params={"q": "alice"})
    assert resp.status_code == 400
    assert "actor_id" in resp.json()["detail"]


def test_C1_search_with_unknown_actor_returns_403(client):
    """Unknown agent_id resolves to GUEST role -> 403. The console's
    membership gate is the source of truth."""
    resp = client.get(
        "/api/agents/search",
        params={"q": "alice", "actor_id": "stranger-no-such-agent"},
    )
    assert resp.status_code == 403


def test_C1_search_with_member_actor_succeeds(client):
    """A registered member can search - normal user flow."""
    client.post("/api/join", json={"agent_id": "alice"})
    resp = client.get(
        "/api/agents/search",
        params={"q": "alice", "actor_id": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert body["query"] == "alice"


# ===== C-1 pubkey redaction =====


def _seed_group_with_member(
    client: TestClient,
    tmp_path: Path,
    member_pk: str = "abcdef0123456789" * 4,  # 64 hex chars
) -> str:
    """Seed a group record on disk with a known pubkey.

    Bypasses GroupRegistry.publish() so we don't need a real Ed25519
    keypair to drive the well-formed / signature gates - the web
    search endpoint only invokes list_all() + from_dict(), which
    tolerates any structurally-valid record on disk.
    """
    import json
    from nth_dao.group_registry import GroupPolicy, GroupRegistry

    registry = GroupRegistry(tmp_path)
    record_path = registry._path_for_slug("qa-team")
    record_path.write_text(
        json.dumps({
            "group_id": "grp-qa-team",
            "slug": "qa-team",
            "display_name": "QA Team",
            "description": "seeded for C-1 test",
            "policy": GroupPolicy.OPEN.value,
            "founder_pubkey": member_pk,
            "member_pubkeys": [member_pk],
            "admin_pubkeys": [member_pk],
            "signer_pubkey": member_pk,
            "sig": "fake-sig-for-test",
            "created_at": "2026-06-07T00:00:00",
            "updated_at": "2026-06-07T00:00:00",
            "metadata": {},
        }),
        encoding="utf-8",
    )
    return member_pk


def test_C1_non_admin_sees_pubkey_prefix_not_full(client, tmp_path):
    """Architect C-1 core: a regular member querying group members
    sees only a 16-char prefix, NOT the full pubkey_hex."""
    full_pk = _seed_group_with_member(client, tmp_path)
    client.post("/api/join", json={"agent_id": "alice"})  # member, not admin

    resp = client.get(
        "/api/agents/search",
        params={"q": full_pk[:8], "actor_id": "alice"},
    )
    assert resp.status_code == 200
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    assert group_rows, "expected at least one group result for our seeded pk"
    row = group_rows[0]
    # The full key MUST NOT leak
    assert "pubkey_hex" not in row
    # The prefix lookup helper IS exposed (16 chars)
    assert row["pubkey_prefix"] == full_pk[:16]


def test_C1_admin_sees_full_pubkey_hex(client, tmp_path):
    """Admins (manage_members permission) still see the full pubkey
    for legitimate admin workflows (revocation, signing audits)."""
    full_pk = _seed_group_with_member(client, tmp_path)

    resp = client.get(
        "/api/agents/search",
        params={"q": full_pk[:8], "actor_id": "admin"},  # default admin
    )
    assert resp.status_code == 200
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    assert group_rows
    row = group_rows[0]
    assert row["pubkey_hex"] == full_pk
    assert row["pubkey_prefix"] == full_pk[:16]


# ===== H-4 dedup namespace =====


def test_H4_pubkey_prefix_collision_with_agent_id_does_not_drop_rows(
    client, tmp_path,
):
    """If a real agent_id ('abcdef0123456789') happens to equal a
    group member's pubkey[:16], pre-fix code's dict[agent_id] collapsed
    them. Now dedup key is (source, agent_id) -> both rows survive."""
    # Make the agent_id match the pubkey prefix
    full_pk = "abcdef0123456789" + "f" * 48
    _seed_group_with_member(client, tmp_path, member_pk=full_pk)
    # Now register a real member with the colliding id
    client.post("/api/join", json={"agent_id": "abcdef0123456789"})

    resp = client.get(
        "/api/agents/search",
        params={"q": "abcdef", "actor_id": "admin"},
    )
    assert resp.status_code == 200
    sources_for_id = {
        r["source"] for r in resp.json()["results"]
        if r.get("agent_id") == "abcdef0123456789"
    }
    # Both rows must be present - one from "home" (member), one from "group"
    assert "home" in sources_for_id, (
        "home (team-member) row was dropped by the bad dedup key"
    )
    assert "group" in sources_for_id, (
        "group (pubkey) row was dropped by the bad dedup key"
    )


# ===== M-1 limit validation =====


def test_M1_limit_non_integer_returns_400_not_500(client):
    """`?limit=foo` should fail with a clean 400, not propagate
    ValueError into a generic 500."""
    client.post("/api/join", json={"agent_id": "alice"})
    resp = client.get(
        "/api/agents/search",
        params={"q": "alice", "limit": "foo", "actor_id": "alice"},
    )
    # FastAPI's automatic type coercion will reject "foo" as int with 422,
    # which is also acceptable (still NOT 500). Either is fine; just not 500.
    assert resp.status_code in (400, 422), (
        f"expected 400/422 on bad limit, got {resp.status_code}"
    )
