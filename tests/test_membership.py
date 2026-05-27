import pytest

import nth_team_layer as nth
from nth_team_layer.membership import JoinPolicy, RequestStatus


def test_init_team_accepts_string_policy_and_admins_are_members(tmp_path):
    team = nth.MembershipManager(tmp_path)

    config = team.init_team(policy="approval", admin_ids=["admin"])

    assert config.join_policy == JoinPolicy.APPROVAL
    assert config.admin_ids == ["admin"]
    assert config.member_ids == ["admin"]


def test_approval_policy_blocks_unapproved_attach(tmp_path):
    admin = nth.MembershipManager(tmp_path)
    admin.init_team(policy="approval", admin_ids=["admin"])

    with pytest.raises(PermissionError, match="approval_required"):
        nth.attach("guest", backend=None, workspace=tmp_path, start_heartbeat=False)

    assert not (tmp_path / "team_agents" / "guest.json").exists()


def test_approval_request_requires_admin_and_then_allows_attach(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="approval", admin_ids=["admin"])

    req = membership.request_join("guest", capabilities=["python"])
    assert req.status == RequestStatus.PENDING

    with pytest.raises(PermissionError):
        membership.approve("guest", reviewed_by="guest")

    approved = membership.approve("guest", reviewed_by="admin")
    assert approved.status == RequestStatus.APPROVED

    session = nth.attach("guest", backend=None, workspace=tmp_path, start_heartbeat=False)
    try:
        assert (tmp_path / "team_agents" / "guest.json").exists()
        assert "guest" in session.membership.load_config().member_ids
    finally:
        session.detach()


def test_open_policy_attach_adds_member(tmp_path):
    session = nth.attach("open-agent", backend=None, workspace=tmp_path, start_heartbeat=False)
    try:
        assert "open-agent" in session.membership.load_config().member_ids
    finally:
        session.detach()


def test_token_policy_attach_requires_valid_token(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="token", join_token="secret", admin_ids=["admin"])

    with pytest.raises(PermissionError, match="invalid_or_missing_token"):
        nth.attach("guest", backend=None, workspace=tmp_path, start_heartbeat=False)

    session = nth.attach(
        "guest",
        backend=None,
        workspace=tmp_path,
        start_heartbeat=False,
        join_token="secret",
    )
    try:
        assert "guest" in session.membership.load_config().member_ids
    finally:
        session.detach()
