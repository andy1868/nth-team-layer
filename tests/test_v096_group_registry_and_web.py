"""v0.9.6 — GroupRegistry uniqueness + governance + Web API additions."""

import pytest

from nth_dao.identity import AgentIdentity, crypto_available


pytestmark = pytest.mark.skipif(
    not crypto_available(), reason="PyNaCl required for v0.9.6 tests"
)


# ─────────────────── slug + uniqueness ───────────────────


def test_normalize_group_name_strips_and_lowercases():
    from nth_dao.group_registry import normalize_group_name
    assert normalize_group_name("Frontend Team!") == "frontend-team"
    assert normalize_group_name("  DAO  Core  ") == "dao-core"
    assert normalize_group_name("foo_bar") == "foo-bar"


def test_normalize_group_name_rejects_too_short():
    from nth_dao.group_registry import GroupRegistryError, normalize_group_name
    with pytest.raises(GroupRegistryError, match="at least"):
        normalize_group_name("a")
    with pytest.raises(GroupRegistryError, match="at least"):
        normalize_group_name("!!")


def test_normalize_group_name_rejects_too_long():
    from nth_dao.group_registry import GroupRegistryError, normalize_group_name
    with pytest.raises(GroupRegistryError, match="at most"):
        normalize_group_name("a" * 100)


def test_group_registry_publishes_first_then_rejects_collision(tmp_path):
    from nth_dao.group_registry import (
        GroupRegistry,
        GroupRegistryError,
        create_group,
    )
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    reg = GroupRegistry(tmp_path)
    g_alice = create_group(alice, display_name="Frontend Team")
    reg.publish(g_alice)
    # bob tries the same slug — collision
    g_bob = create_group(bob, display_name="frontend  team!")
    with pytest.raises(GroupRegistryError, match="already taken"):
        reg.publish(g_bob)


def test_group_registry_load_by_slug_or_id(tmp_path):
    from nth_dao.group_registry import GroupRegistry, create_group
    alice = AgentIdentity.generate(label="alice")
    reg = GroupRegistry(tmp_path)
    g = create_group(alice, display_name="DAO Builders")
    reg.publish(g)
    by_slug = reg.load_by_slug("DAO Builders")
    assert by_slug is not None and by_slug.group_id == g.group_id
    by_id = reg.load_by_id(g.group_id)
    assert by_id is not None and by_id.slug == "dao-builders"


def test_group_registry_search_fuzzy(tmp_path):
    from nth_dao.group_registry import GroupRegistry, create_group
    alice = AgentIdentity.generate(label="alice")
    reg = GroupRegistry(tmp_path)
    reg.publish(create_group(alice, display_name="Frontend Team"))
    reg.publish(create_group(alice, display_name="Backend Team"))
    reg.publish(create_group(alice, display_name="DAO Builders"))
    hits = reg.search("team")
    ids = {h.slug for h in hits}
    assert "frontend-team" in ids and "backend-team" in ids
    assert "dao-builders" not in ids


def test_group_registry_rejects_update_by_newly_claimed_admin(tmp_path):
    from nth_dao.group_registry import GroupRegistry, GroupRecord, GroupRegistryError, create_group
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    reg = GroupRegistry(tmp_path)
    original = create_group(alice, display_name="Governance Team")
    reg.publish(original)

    malicious = GroupRecord.from_dict(original.to_dict())
    malicious.member_pubkeys = [alice.pubkey_hex, bob.pubkey_hex]
    malicious.admin_pubkeys = [alice.pubkey_hex, bob.pubkey_hex]
    malicious.signer_pubkey = bob.pubkey_hex
    malicious.sig = bob.sign_json(malicious.signable_dict())

    with pytest.raises(GroupRegistryError, match="current group admins"):
        reg.publish(malicious)


def test_group_registry_rejects_signer_outside_admin_set(tmp_path):
    from nth_dao.group_registry import GroupRegistry, GroupRecord, GroupRegistryError, create_group
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    reg = GroupRegistry(tmp_path)
    record = GroupRecord.from_dict(create_group(alice, display_name="Signer Team").to_dict())
    record.signer_pubkey = bob.pubkey_hex
    record.sig = bob.sign_json(record.signable_dict())

    with pytest.raises(GroupRegistryError, match="malformed"):
        reg.publish(record)


# ─────────────────── governance / voting ───────────────────


def test_proposal_passes_with_majority_yes(tmp_path):
    from nth_dao.group_registry import (
        GroupPolicy,
        apply_proposal,
        cast_vote,
        create_group,
        propose_policy_change,
        resolve_proposal,
    )
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    carol = AgentIdentity.generate(label="carol")
    g = create_group(alice, display_name="DAO Team", policy=GroupPolicy.OPEN,
                     initial_admin_pubkeys=[bob.pubkey_hex, carol.pubkey_hex])
    # 3 admins → 3 members. Majority = 2.
    assert len(g.member_pubkeys) == 3

    proposal = propose_policy_change(
        alice, g, new_policy="closed", rationale="freeze membership",
    )
    proposal.votes.append(cast_vote(alice, proposal, choice="yes"))
    proposal.votes.append(cast_vote(bob, proposal, choice="yes"))
    passed, reason = resolve_proposal(proposal, g)
    assert passed, reason

    updated = apply_proposal(alice, proposal, g)
    assert updated.policy == GroupPolicy.CLOSED
    # Founder & created_at preserved
    assert updated.created_at == g.created_at
    assert updated.founder_pubkey == g.founder_pubkey


def test_proposal_rejected_below_threshold(tmp_path):
    from nth_dao.group_registry import (
        cast_vote,
        create_group,
        propose_policy_change,
        resolve_proposal,
    )
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    carol = AgentIdentity.generate(label="carol")
    g = create_group(alice, display_name="A Big Team",
                     initial_admin_pubkeys=[bob.pubkey_hex, carol.pubkey_hex])
    proposal = propose_policy_change(alice, g, new_policy="closed")
    proposal.votes.append(cast_vote(alice, proposal, choice="yes"))
    # 1 of 3, need 2
    passed, _ = resolve_proposal(proposal, g)
    assert not passed


def test_proposal_rejects_non_member_proposer(tmp_path):
    from nth_dao.group_registry import (
        GroupRegistryError,
        create_group,
        propose_policy_change,
    )
    alice = AgentIdentity.generate(label="alice")
    mallory = AgentIdentity.generate(label="mallory (outsider)")
    g = create_group(alice, display_name="Insider Team")
    with pytest.raises(GroupRegistryError, match="current members"):
        propose_policy_change(mallory, g, new_policy="open")


def test_proposal_dedupes_double_yes_vote(tmp_path):
    from nth_dao.group_registry import (
        cast_vote,
        create_group,
        propose_policy_change,
        resolve_proposal,
    )
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    g = create_group(alice, display_name="Two Admin Team",
                     initial_admin_pubkeys=[bob.pubkey_hex])
    proposal = propose_policy_change(alice, g, new_policy="closed")
    # alice votes yes TWICE — second is dedup'd
    proposal.votes.append(cast_vote(alice, proposal, choice="yes"))
    proposal.votes.append(cast_vote(alice, proposal, choice="yes"))
    passed, reason = resolve_proposal(proposal, g)
    assert not passed
    assert "1/2" in reason


def test_proposal_tampered_signature_rejected(tmp_path):
    from nth_dao.group_registry import (
        cast_vote,
        create_group,
        propose_policy_change,
        resolve_proposal,
    )
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    g = create_group(alice, display_name="Tampered Team",
                     initial_admin_pubkeys=[bob.pubkey_hex])
    proposal = propose_policy_change(alice, g, new_policy="closed")
    proposal.votes.append(cast_vote(alice, proposal, choice="yes"))
    proposal.votes.append(cast_vote(bob, proposal, choice="yes"))
    # tamper the proposer's signature
    proposal.proposer_sig = "00" * 64
    passed, reason = resolve_proposal(proposal, g)
    assert not passed
    assert "signature" in reason.lower()


# ─────────────────── Web API ───────────────────


def _client(tmp_path):
    from fastapi.testclient import TestClient
    from nth_dao.web import create_app
    app = create_app(tmp_path)
    return TestClient(app)


def test_web_search_agents_returns_ranked_results(tmp_path):
    from nth_dao.discovery.agent_registry import AgentRecord, AgentRegistry
    from nth_dao.util import atomic_write_json
    reg = AgentRegistry(agents_dir=str(tmp_path / "team_agents"))
    for ar in [
        AgentRecord(agent_id="alice-prod", hostname="h", pid=1,
                    capabilities=["python", "web"],
                    metadata={"identity": {"label": "Alice 王"}}),
        AgentRecord(agent_id="alice-dev",  hostname="h", pid=2,
                    capabilities=["python"]),
        AgentRecord(agent_id="bob",        hostname="h", pid=3,
                    capabilities=["rust"]),
    ]:
        atomic_write_json(reg._path_for(ar.agent_id), ar.to_dict())
    client = _client(tmp_path)
    resp = client.get("/api/agents/search", params={"q": "ali"})
    assert resp.status_code == 200
    data = resp.json()
    ids = [r["agent_id"] for r in data["results"]]
    assert "alice-prod" in ids
    assert "alice-dev" in ids
    assert "bob" not in ids


def test_web_search_agents_empty_query_returns_empty(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/agents/search", params={"q": ""})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_web_group_create_then_search(tmp_path):
    """Full TS-style flow: prepare unsigned → sign locally → publish → search."""
    from nth_dao.group_registry import GroupRecord
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    # Step 1: ask the server for an unsigned skeleton.
    resp = client.post("/api/groups/registry", json={
        "actor_id": "admin",
        "actor_pubkey_hex": alice.pubkey_hex,
        "display_name": "Privacy Working Group",
        "description": "Discuss privacy stuff.",
        "policy": "approval",
    })
    assert resp.status_code == 200, resp.text
    unsigned = resp.json()["unsigned_record"]
    # Step 2: client signs the canonical payload.
    import uuid as _uuid
    unsigned["group_id"] = _uuid.uuid4().hex[:12]
    record = GroupRecord.from_dict(unsigned)
    record.sig = alice.sign_json(record.signable_dict())
    # Step 3: publish.
    resp2 = client.post("/api/groups/registry/publish",
                        json={"record": record.to_dict()})
    assert resp2.status_code == 200, resp2.text
    # Step 4: search.
    resp3 = client.post("/api/groups/registry/search",
                        json={"query": "privacy"})
    assert resp3.status_code == 200
    results = resp3.json()["results"]
    assert len(results) >= 1
    assert results[0]["slug"] == "privacy-working-group"


def test_web_group_publish_rejects_invalid_signature(tmp_path):
    from nth_dao.group_registry import GroupPolicy, GroupRecord
    client = _client(tmp_path)
    bad_record = GroupRecord(
        group_id="abc12345", slug="bad-group",
        display_name="Bad Group", description="",
        policy=GroupPolicy.OPEN,
        founder_pubkey="00" * 32,
        member_pubkeys=["00" * 32],
        admin_pubkeys=["00" * 32],
        signer_pubkey="00" * 32,
        sig="ff" * 64,   # not a real signature
    )
    resp = client.post("/api/groups/registry/publish",
                       json={"record": bad_record.to_dict()})
    assert resp.status_code == 409  # GroupRegistryError → 409 per route impl


def test_web_group_publish_requires_client_signed_group_id(tmp_path):
    from nth_dao.group_registry import GroupRecord
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    resp = client.post("/api/groups/registry", json={
        "actor_id": "admin",
        "actor_pubkey_hex": alice.pubkey_hex,
        "display_name": "Unsigned Id Group",
        "policy": "open",
    })
    assert resp.status_code == 200
    record = GroupRecord.from_dict(resp.json()["unsigned_record"])
    record.sig = alice.sign_json(record.signable_dict())
    publish = client.post("/api/groups/registry/publish", json={"record": record.to_dict()})
    assert publish.status_code == 400
    assert "group_id" in publish.text


def test_web_add_did_derives_agent_id_like_identity_layer(tmp_path):
    from nth_dao.did_key import encode_ed25519_did_key_hex
    from nth_dao.identity import AgentID
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    did = encode_ed25519_did_key_hex(alice.pubkey_hex)
    resp = client.post("/api/agents/add", json={
        "actor_id": "admin",
        "target_did": did,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == str(AgentID.from_pubkey(alice.pubkey_hex))


def test_web_vote_endpoint_returns_unsigned_vote_without_persisting(tmp_path):
    from nth_dao.group_registry import GroupRegistry, create_group, propose_policy_change
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    group = create_group(alice, display_name="Vote Prep Team", initial_admin_pubkeys=[bob.pubkey_hex])
    reg = GroupRegistry(tmp_path)
    reg.publish(group)
    proposal = propose_policy_change(alice, group, new_policy="closed")
    reg.save_proposal(proposal)

    resp = client.post(
        f"/api/groups/registry/{group.group_id}/proposals/{proposal.proposal_id}/vote",
        json={"voter_pubkey_hex": bob.pubkey_hex, "proposal_id": proposal.proposal_id, "choice": "yes"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["unsigned_vote"]["sig"] == ""
    stored = reg.load_proposal(proposal.proposal_id)
    assert stored is not None
    assert stored.votes == []


def test_web_signed_vote_rejects_bad_signature_and_accepts_valid_vote(tmp_path):
    from nth_dao.group_registry import GroupRegistry, cast_vote, create_group, propose_policy_change
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    group = create_group(alice, display_name="Signed Vote Team", initial_admin_pubkeys=[bob.pubkey_hex])
    reg = GroupRegistry(tmp_path)
    reg.publish(group)
    proposal = propose_policy_change(alice, group, new_policy="closed")
    reg.save_proposal(proposal)

    bad_vote = cast_vote(bob, proposal, choice="yes")
    bad_vote["sig"] = "00" * 64
    bad = client.post(
        f"/api/groups/registry/{group.group_id}/proposals/{proposal.proposal_id}/sign_vote",
        json={"vote": bad_vote},
    )
    assert bad.status_code == 400

    good_vote = cast_vote(bob, proposal, choice="yes")
    good = client.post(
        f"/api/groups/registry/{group.group_id}/proposals/{proposal.proposal_id}/sign_vote",
        json={"vote": good_vote},
    )
    assert good.status_code == 200, good.text
    stored = reg.load_proposal(proposal.proposal_id)
    assert stored is not None
    assert stored.votes == [good_vote]


def test_web_signed_vote_rejects_non_member(tmp_path):
    from nth_dao.group_registry import GroupRegistry, cast_vote, create_group, propose_policy_change
    client = _client(tmp_path)
    alice = AgentIdentity.generate(label="alice")
    mallory = AgentIdentity.generate(label="mallory")
    group = create_group(alice, display_name="Member Vote Team")
    reg = GroupRegistry(tmp_path)
    reg.publish(group)
    proposal = propose_policy_change(alice, group, new_policy="closed")
    reg.save_proposal(proposal)

    vote = cast_vote(mallory, proposal, choice="yes")
    resp = client.post(
        f"/api/groups/registry/{group.group_id}/proposals/{proposal.proposal_id}/sign_vote",
        json={"vote": vote},
    )
    assert resp.status_code == 400
    assert "current member" in resp.text


def test_web_lan_discover_runs_without_error(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/agents/lan_discover",
                       json={"timeout_seconds": 0.5})
    assert resp.status_code == 200
    assert "peers" in resp.json()


def test_facade_exports_v096():
    import nth_dao as nth
    from nth_dao.group_registry import GroupRegistry
    # Module accessible via attribute access
    assert nth.group_registry.GroupRegistry is GroupRegistry
