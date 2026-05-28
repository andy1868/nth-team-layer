import pytest

import nth_dao as nth


def test_channel_messages_and_audit_are_local_first(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="open", admin_ids=["admin"])
    membership.ensure_member("alice")
    groups = nth.GroupManager(tmp_path, membership=membership)

    channel = groups.create_channel("general", created_by="alice", topic="team chat")
    message = groups.post_message(channel.channel_id, sender_id="alice", body="hello team")

    messages = groups.list_messages(channel.channel_id, actor_id="alice")
    events = groups.list_audit_events()

    assert channel.channel_id == "general"
    assert messages[0].message_id == message.message_id
    assert messages[0].body == "hello team"
    assert [event.event_type for event in events] == [
        "channel.created",
        "message.posted",
    ]


def test_private_channel_blocks_non_members(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="open", admin_ids=["admin"])
    membership.ensure_member("alice")
    membership.ensure_member("bob")
    groups = nth.GroupManager(tmp_path, membership=membership)

    groups.create_channel(
        "secret",
        created_by="alice",
        is_private=True,
        member_ids=["alice"],
    )
    groups.post_message("secret", sender_id="alice", body="private")

    with pytest.raises(PermissionError):
        groups.list_messages("secret", actor_id="bob")


def test_announcements_and_trust_hints_require_admin_permissions(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="open", admin_ids=["admin"])
    membership.ensure_member("member")
    groups = nth.GroupManager(tmp_path, membership=membership)

    with pytest.raises(PermissionError):
        groups.post_announcement("Rules", "Be kind", author_id="member")

    announcement = groups.post_announcement("Rules", "Be kind", author_id="admin")
    hint = groups.set_trust_hint(
        "member",
        score=0.7,
        label="reliable",
        reason="completed review",
        source_id="admin",
    )

    assert announcement.title == "Rules"
    assert groups.list_announcements()[0].announcement_id == announcement.announcement_id
    assert hint.score == 0.7
    assert groups.get_trust_hint("member").label == "reliable"


def test_tasks_have_simple_status_permissions(tmp_path):
    membership = nth.MembershipManager(tmp_path)
    membership.init_team(policy="open", admin_ids=["admin"])
    membership.ensure_member("alice")
    membership.ensure_member("bob")
    groups = nth.GroupManager(tmp_path, membership=membership)

    task = groups.create_task(
        "write adapter",
        created_by="alice",
        assignee_id="bob",
        description="A2A adapter spike",
    )

    with pytest.raises(PermissionError):
        groups.update_task_status(task.task_id, nth.TaskStatus.RUNNING, actor_id="stranger")

    updated = groups.update_task_status(
        task.task_id,
        nth.TaskStatus.RUNNING,
        actor_id="bob",
        note="starting",
    )

    assert updated.status == nth.TaskStatus.RUNNING
    assert updated.metadata["last_note"] == "starting"
    assert groups.list_tasks(status="running")[0].task_id == task.task_id


def test_membership_request_alias_is_exported():
    assert nth.MembershipRequest is nth.JoinRequest
