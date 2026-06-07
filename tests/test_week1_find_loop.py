"""Week-1 find-loop regression net.

Pin the end-to-end "find friend" path the user should be able to drive
from the dashboard:

    (a) admin uses search box -> backend returns home/registry/group results
    (b) admin clicks "+ Add" on a result -> backend ensure_member persists
    (c) admin (or anyone) searches again for the same name -> result hits

The integration concern this catches: a regression that decouples
``/api/agents/add`` from ``/api/agents/search`` - either side getting
moved without the other (e.g. add starts writing to a new
``addressbook.json`` but search keeps reading only ``team.json``) would
leave the loop visibly broken on the UI but pass each endpoint's unit
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


# ===== closed-loop =====


def test_W1_add_then_search_immediately_finds_the_added_agent(client):
    """The minimum viable find-loop: search empty -> add -> search hits."""
    # Step 1: alice is unknown -> search returns no alice rows
    pre = client.get(
        "/api/agents/search",
        params={"q": "alice", "actor_id": "admin"},
    )
    assert pre.status_code == 200
    pre_ids = [r["agent_id"] for r in pre.json()["results"]]
    assert "alice" not in pre_ids

    # Step 2: admin adds alice
    add = client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "alice"},
    )
    assert add.status_code == 200, add.text
    assert add.json()["ok"] is True
    assert add.json()["agent_id"] == "alice"

    # Step 3: search finds her on the next call (no restart, no refresh)
    post = client.get(
        "/api/agents/search",
        params={"q": "ali", "actor_id": "admin"},
    )
    assert post.status_code == 200
    post_rows = post.json()["results"]
    alice_rows = [r for r in post_rows if r["agent_id"] == "alice"]
    assert len(alice_rows) >= 1, (
        f"add->search loop broken; got results: {post_rows}"
    )


def test_W1_added_agent_appears_with_home_source_and_a_code(client):
    """Added agents surface with source='home' (they live in the team
    config, not the LAN registry) AND a Telegram-style code so the
    operator can confirm "this is who I just added"."""
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "bob"},
    )
    resp = client.get(
        "/api/agents/search",
        params={"q": "bob", "actor_id": "admin"},
    )
    bob_rows = [r for r in resp.json()["results"] if r["agent_id"] == "bob"]
    assert len(bob_rows) == 1
    row = bob_rows[0]
    assert row["source"] == "home"
    assert row["code"], f"row missing code for UI display: {row}"
    # Code shape: "abcd-efgh" - 9 chars with a hyphen at index 4
    assert "-" in row["code"]


def test_W1_search_admin_finds_self_via_home_source(client):
    """Sanity: searching for the default admin must return at least one
    row pointing at admin via the home source. This is the smoke test
    the operator runs first when opening the dashboard."""
    resp = client.get(
        "/api/agents/search",
        params={"q": "admin", "actor_id": "admin"},
    )
    rows = resp.json()["results"]
    admin_home = [
        r for r in rows
        if r["agent_id"] == "admin" and r["source"] == "home"
    ]
    assert len(admin_home) == 1
    assert admin_home[0]["role"] == "owner"


def test_W1_add_endpoint_persists_across_multiple_search_calls(client):
    """Five back-to-back searches all see the same added agent - rules
    out a transient cache that decays inside the same TestClient session."""
    client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "charlie"},
    )
    for _ in range(5):
        resp = client.get(
            "/api/agents/search",
            params={"q": "char", "actor_id": "admin"},
        )
        hits = [r for r in resp.json()["results"] if r["agent_id"] == "charlie"]
        assert len(hits) == 1
