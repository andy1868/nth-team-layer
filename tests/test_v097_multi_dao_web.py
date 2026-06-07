"""v0.9.7 — multi-DAO sidebar endpoints.

Backs the chat-native "My DAOs" list and per-DAO state fetch. Each registered
GroupRegistry record shows up as a DAO; the local workspace is the always-
present "home" DAO. Group DAOs own channels prefixed `dao-<slug>-`.

These tests are written against the FastAPI TestClient so they cover the
public API contract — exactly what the React frontend talks to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.group_registry import GroupPolicy, GroupRecord
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(workspace=tmp_path)
    return TestClient(app)


@pytest.fixture
def founder():
    if not crypto_available():
        pytest.skip("PyNaCl required for signed group creation")
    return AgentIdentity.generate(label="founder")


def _register_group(client: TestClient, founder: AgentIdentity, *,
                    display_name: str, description: str = "",
                    policy: str = "open") -> dict:
    """Drive the same create→sign→publish flow the frontend uses.

    The local workspace admin ("admin", seeded by bootstrap) calls the
    prepare endpoint on behalf of the would-be founder, who supplies their
    own pubkey via `actor_pubkey_hex`. The skeleton is then signed locally
    by the founder and posted back to publish.
    """
    prep = client.post(
        "/api/groups/registry",
        json={
            "actor_id": "admin",
            "actor_pubkey_hex": founder.pubkey_hex,
            "display_name": display_name,
            "description": description,
            "policy": policy,
        },
    )
    assert prep.status_code == 200, prep.text
    skeleton = prep.json()["unsigned_record"]
    import secrets
    skeleton["group_id"] = secrets.token_hex(6)
    record = GroupRecord.from_dict(skeleton)
    record.sig = founder.sign_json(record.signable_dict())
    pub = client.post(
        "/api/groups/registry/publish",
        json={"record": record.to_dict()},
    )
    assert pub.status_code == 200, pub.text
    return pub.json()


def test_my_daos_lists_home_only_initially(client: TestClient):
    res = client.get("/api/daos", params={"actor_id": "admin"})
    assert res.status_code == 200
    daos = res.json()["daos"]
    assert len(daos) == 1
    assert daos[0]["slug"] == "home"
    assert daos[0]["kind"] == "home"
    assert daos[0]["joined"] is True   # bootstrap added admin as owner


def test_my_daos_includes_registered_group_as_not_joined(client: TestClient, founder):
    _register_group(client, founder, display_name="MumoLawOS",
                    description="Legal-tech DAO for autonomous court agents")
    # An admin who is not in MumoLawOS's pubkey set sees it as joinable.
    res = client.get(
        "/api/daos",
        params={"actor_id": "admin", "actor_pubkey_hex": "ff" * 32},
    )
    assert res.status_code == 200
    daos = res.json()["daos"]
    slugs = {d["slug"] for d in daos}
    assert "home" in slugs
    assert "mumolawos" in slugs
    mumo = next(d for d in daos if d["slug"] == "mumolawos")
    assert mumo["kind"] == "group"
    assert mumo["display_name"] == "MumoLawOS"
    assert mumo["joined"] is False
    assert mumo["member_count"] >= 1   # founder is in the member set


def test_my_daos_marks_joined_when_actor_pubkey_matches(client: TestClient, founder):
    _register_group(client, founder, display_name="Privacy WG")
    res = client.get(
        "/api/daos",
        params={"actor_id": str(founder.agent_id),
                "actor_pubkey_hex": founder.pubkey_hex},
    )
    assert res.status_code == 200
    daos = res.json()["daos"]
    pwg = next(d for d in daos if d["slug"] == "privacy-wg")
    assert pwg["joined"] is True


def test_dao_state_home_passthrough(client: TestClient):
    res = client.get("/api/daos/home/state", params={"agent_id": "admin"})
    assert res.status_code == 200
    body = res.json()
    assert body["dao"]["kind"] == "home"
    assert body["dao"]["slug"] == "home"
    # Home dao keeps legacy channels (no `dao-` prefix).
    assert all(not c["channel_id"].startswith("dao-") for c in body["channels"])
    assert body["active_channel_id"] == "general"


def test_dao_state_for_group_isolates_channels(client: TestClient, founder):
    _register_group(client, founder, display_name="MumoLawOS")
    # Pre-create one home-scoped channel and one group-scoped channel directly.
    client.post(
        "/api/channels",
        json={"actor_id": "admin", "name": "general", "channel_id": "home-general"},
    )
    client.post(
        "/api/channels",
        json={
            "actor_id": "admin",
            "name": "lounge",
            "channel_id": "dao-mumolawos-lounge",
        },
    )

    # /home/state shows home channels but NOT the mumolawos-scoped one.
    home = client.get("/api/daos/home/state", params={"agent_id": "admin"}).json()
    home_ids = {c["channel_id"] for c in home["channels"]}
    assert "dao-mumolawos-lounge" not in home_ids
    assert "home-general" in home_ids

    # /mumolawos/state shows ONLY the dao-prefixed channel.
    mumo = client.get(
        "/api/daos/mumolawos/state", params={"agent_id": "admin"}
    ).json()
    assert mumo["dao"]["kind"] == "group"
    assert mumo["dao"]["slug"] == "mumolawos"
    ids = {c["channel_id"] for c in mumo["channels"]}
    assert ids == {"dao-mumolawos-lounge"}
    # Default channel for a group resolves to dao-<slug>-general.
    assert mumo["active_channel_id"] == "dao-mumolawos-general"


def test_dao_state_unknown_slug_returns_404(client: TestClient):
    res = client.get("/api/daos/does-not-exist/state", params={"agent_id": "admin"})
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_dao_scoped_channel_create_autoprefixes(client: TestClient, founder):
    _register_group(client, founder, display_name="MumoLawOS")
    # Without manual channel_id, the DAO-scoped endpoint inserts the prefix.
    created = client.post(
        "/api/daos/mumolawos/channels",
        json={"actor_id": "admin", "name": "court-room"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["channel_id"].startswith("dao-mumolawos-")


def test_dao_scoped_message_rejects_wrong_channel(client: TestClient, founder):
    _register_group(client, founder, display_name="MumoLawOS")
    # First create a home-scoped channel that the message tries to abuse.
    client.post(
        "/api/channels",
        json={"actor_id": "admin", "name": "general", "channel_id": "general"},
    )
    bad = client.post(
        "/api/daos/mumolawos/messages",
        json={"agent_id": "admin", "channel_id": "general", "body": "leak"},
    )
    assert bad.status_code == 400
    assert "must start with" in bad.json()["detail"]


def test_mumolawos_now_openable(client: TestClient, founder):
    """Regression: an end-user created a 'MumoLawOS' DAO via /groups/registry
    and previously couldn't click into it because there was no DAO endpoint.
    With v0.9.7 the slug resolves and returns metadata + empty channels."""
    _register_group(client, founder, display_name="MumoLawOS",
                    description="Project DAO for legal AI agents.")
    res = client.get("/api/daos/mumolawos/state", params={"agent_id": "admin"})
    assert res.status_code == 200
    body = res.json()
    assert body["dao"]["display_name"] == "MumoLawOS"
    assert body["dao"]["description"] == "Project DAO for legal AI agents."
    # Members come from the GroupRecord pubkey set, not the workspace.
    assert len(body["members"]) >= 1
    # No channels yet — the UI shows the "create the first channel" hint.
    assert body["channels"] == []
