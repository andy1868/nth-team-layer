import json

import nth_team_layer as nth


def test_plain_identity_round_trip(tmp_path):
    identity_path = tmp_path / ".nth" / "identity.json"
    identity = nth.AgentIdentity.from_string(
        "alice",
        label="Alice",
        metadata={"role": "reviewer"},
    )

    identity.save(identity_path)
    loaded = nth.AgentIdentity.load(identity_path)

    assert str(loaded.agent_id) == "alice"
    assert loaded.label == "Alice"
    assert loaded.metadata["role"] == "reviewer"
    assert loaded.public_dict()["is_cryptographic"] is False


def test_load_or_generate_creates_stable_identity_file(tmp_path):
    first = nth.load_or_generate(tmp_path, label="worker")
    second = nth.load_or_generate(tmp_path, label="ignored")

    assert str(first.agent_id) == str(second.agent_id)
    assert nth.default_identity_path(tmp_path).exists()


def test_attach_exports_identity_metadata_without_bypassing_membership(tmp_path):
    identity = nth.AgentIdentity.from_string("alice", label="Alice")
    session = nth.attach(
        "alice",
        backend=None,
        workspace=tmp_path,
        start_heartbeat=False,
        identity=identity,
    )
    try:
        record_path = tmp_path / "team_agents" / "alice.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))

        assert session.identity is identity
        assert record["metadata"]["identity"]["agent_id"] == "alice"
        assert record["metadata"]["identity"]["label"] == "Alice"
        assert "alice" in session.membership.load_config().member_ids
    finally:
        session.detach()


def test_identity_does_not_bypass_approval_policy(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="approval", admin_ids=["admin"])

    identity = nth.AgentIdentity.from_string("guest", label="Guest")

    try:
        nth.attach(
            "guest",
            backend=None,
            workspace=tmp_path,
            start_heartbeat=False,
            identity=identity,
        )
    except PermissionError as exc:
        assert "approval_required" in str(exc)
    else:
        raise AssertionError("unapproved identity attach should be blocked")
