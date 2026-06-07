"""v0.9.8 — visible agent codes + demo responder.

Two UX gaps the user reported:

1. Agents in the UI had no distinguishing visible identifier. Members
   showed only short ``agent_id`` fragments; users couldn't tell who's
   who or paste a handle for "add by code".

2. After creating a DAO and sending a message, nothing replied. The
   loop "user types → agent answers" was open.

This suite locks both in: code derivation is stable + reversible by
``parse_code``, the search endpoint resolves codes back to agents,
and the demo responder posts a visible reply on every user message
in a responding DAO without looping on its own replies.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.agent_code import (
    CODE_LEN,
    code_for_agent_id,
    code_for_pubkey,
    parse_code,
)
from nth_dao.demo_responder import (
    DEFAULT_AGENT_ID,
    ResponderContext,
    compose_reply,
    is_responder_dao,
    maybe_reply,
    set_compose_reply,
)
from nth_dao.group_registry import GroupRecord
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.web import create_app


# ─── agent_code module ──────────────────────────────────────────────────


def test_code_for_pubkey_is_stable_and_formatted():
    pk = "a3" * 32
    code = code_for_pubkey(pk)
    # 8 hex + one dash → 9 chars
    assert len(code) == CODE_LEN + 1
    assert code[4] == "-"
    # Stability: same pubkey, same code
    assert code_for_pubkey(pk) == code


def test_code_for_pubkey_empty_returns_empty():
    assert code_for_pubkey("") == ""


def test_code_for_agent_id_stable():
    code = code_for_agent_id("alice")
    assert "-" in code
    assert code_for_agent_id("alice") == code
    assert code_for_agent_id("bob") != code  # different inputs differ


def test_parse_code_accepts_with_and_without_dash():
    assert parse_code("a3f7-b2e8") == "a3f7b2e8"
    assert parse_code("a3f7b2e8") == "a3f7b2e8"
    assert parse_code("A3F7-B2E8") == "a3f7b2e8"
    assert parse_code("  a3f7-b2e8  ") == "a3f7b2e8"


def test_parse_code_rejects_bad_inputs():
    for bad in ("", "xyz", "abc", "12345678901234567", "a3f7-b2", "g3f7-b2e8"):
        with pytest.raises(ValueError):
            parse_code(bad)


def test_parse_code_rejects_non_string():
    with pytest.raises(ValueError):
        parse_code(12345)   # type: ignore[arg-type]


def test_code_round_trip():
    """parse_code(code_for_*(input)) is the dash-less form of the same code."""
    pk = "b1" * 32
    code = code_for_pubkey(pk)
    assert parse_code(code) == code.replace("-", "")


# ─── demo_responder module ─────────────────────────────────────────────


def test_is_responder_dao_defaults():
    # "demo" / "test" anywhere opts in
    assert is_responder_dao("demo-dao") is True
    assert is_responder_dao("test-team") is True
    # Open policy opts in even without "demo" in name
    assert is_responder_dao("regular", policy="open") is True
    # Closed groups do NOT trigger
    assert is_responder_dao("private-wg", policy="closed") is False
    # Empty slug never triggers
    assert is_responder_dao("") is False


def test_compose_reply_quotes_user_message():
    ctx = ResponderContext(
        dao_slug="demo",
        channel_id="general",
        sender_id="alice",
        sender_code="2bd8-04ee",
        body="hello world",
    )
    reply = compose_reply(ctx)
    assert "hello world" in reply
    assert "EchoAgent" in reply
    assert "2bd8-04ee" in reply  # uses code over raw id when available


def test_compose_reply_truncates_long_input():
    ctx = ResponderContext(
        dao_slug="demo",
        channel_id="general",
        sender_id="x",
        sender_code="",
        body="A" * 5000,
    )
    reply = compose_reply(ctx)
    # The snippet inside the reply must be bounded
    assert "A" * 5000 not in reply
    assert "A" * 100 in reply       # at least some of it appears


def test_set_compose_reply_overrides_default():
    set_compose_reply(lambda ctx: f"custom:{ctx.body}")
    ctx = ResponderContext("demo", "general", "alice", "2bd8-04ee", "hi")
    assert compose_reply(ctx) == "custom:hi"
    # Restore default for other tests
    from nth_dao.demo_responder import DEFAULT_REPLY_TEMPLATE

    def restored(ctx: ResponderContext) -> str:
        return DEFAULT_REPLY_TEMPLATE.format(
            sender=(ctx.sender_code or ctx.sender_id or "friend"),
            snippet=(ctx.body or "").strip()[:160] or "(empty)",
        )
    set_compose_reply(restored)


def test_maybe_reply_skips_when_sender_is_responder():
    class FakeGroups:
        def post_message(self, *a, **kw):
            raise AssertionError("must not post when sender == responder")
    out = maybe_reply(
        FakeGroups(),
        dao_slug="demo",
        channel_id="general",
        sender_id=DEFAULT_AGENT_ID,
        body="should be ignored",
        responder_id=DEFAULT_AGENT_ID,
    )
    assert out is None


def test_maybe_reply_skips_when_dao_opts_out():
    class FakeGroups:
        def post_message(self, *a, **kw):
            raise AssertionError("must not post when DAO opts out")
    out = maybe_reply(
        FakeGroups(),
        dao_slug="private-wg",
        channel_id="general",
        sender_id="alice",
        body="hi",
        dao_policy="closed",
        dao_description="quiet group",
    )
    assert out is None


def test_maybe_reply_swallows_groupmanager_failure():
    class BoomGroups:
        def post_message(self, *a, **kw):
            raise RuntimeError("disk full")
    # Must NOT raise — responder failure shouldn't break the user's primary post.
    out = maybe_reply(
        BoomGroups(),
        dao_slug="demo",
        channel_id="general",
        sender_id="alice",
        body="hi",
    )
    assert out is None


def test_maybe_reply_skips_empty_body():
    class FakeGroups:
        def post_message(self, *a, **kw):
            raise AssertionError("must not post for empty body")
    for body in ("", "   ", "\n"):
        assert maybe_reply(
            FakeGroups(),
            dao_slug="demo",
            channel_id="general",
            sender_id="alice",
            body=body,
        ) is None


# ─── web integration ────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(workspace=tmp_path))


def test_summary_exposes_actor_code(client):
    res = client.get("/api/summary", params={"actor_id": "alice"})
    assert res.status_code == 200
    body = res.json()
    assert "actor_code" in body
    assert body["actor_code"] == code_for_agent_id("alice")


def test_state_actor_carries_code(client):
    res = client.get("/api/state", params={"agent_id": "admin"})
    assert res.status_code == 200
    body = res.json()
    identity = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert body["actor"]["code"] == identity["code"]
    assert body["actor"]["code"] != code_for_agent_id("admin")


def test_state_members_carry_code(client):
    res = client.get("/api/state", params={"agent_id": "admin"})
    assert res.status_code == 200
    identity = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    for member in res.json()["members"]:
        assert "code" in member
        if member["agent_id"] == "admin":
            assert member["code"] == identity["code"]
            assert member["code"] != code_for_agent_id("admin")
        else:
            assert member["code"] == code_for_agent_id(member["agent_id"])


def test_lookup_by_code_finds_home_member(client):
    # R-35 (2026-06-08): code is now derived from the bootstrap
    # admin's pubkey, not the literal "admin" string. Look up the
    # actual code via /api/identity instead of computing the legacy
    # constant.
    code = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    res = client.get(f"/api/agents/by_code/{code}", params={"actor_id": "admin"})
    assert res.status_code == 200
    body = res.json()
    assert body["agent_id"] == "admin"
    assert body["source"] == "home"


def test_lookup_by_code_accepts_dashless(client):
    # R-35 (2026-06-08): same code source as above; we just strip
    # the dash to exercise the by_code parser's leniency.
    code = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"].replace("-", "")
    res = client.get(f"/api/agents/by_code/{code}", params={"actor_id": "admin"})
    assert res.status_code == 200


def test_lookup_by_code_404_on_unknown(client):
    res = client.get(
        "/api/agents/by_code/aaaa-bbbb", params={"actor_id": "admin"}
    )
    assert res.status_code == 404


def test_lookup_by_code_rejects_bad_format(client):
    res = client.get(
        "/api/agents/by_code/not-a-code", params={"actor_id": "admin"}
    )
    assert res.status_code == 400


def test_lookup_by_code_finds_group_member(client):
    if not crypto_available():
        pytest.skip("PyNaCl required for group fixture")
    founder = AgentIdentity.generate(label="founder")
    prep = client.post("/api/groups/registry", json={
        "actor_id": "admin",
        "actor_pubkey_hex": founder.pubkey_hex,
        "display_name": "MumoLawOS",
        "description": "Legal-tech demo",
        "policy": "open",
    })
    assert prep.status_code == 200, prep.text
    skeleton = prep.json()["unsigned_record"]
    skeleton["group_id"] = secrets.token_hex(6)
    rec = GroupRecord.from_dict(skeleton)
    rec.sig = founder.sign_json(rec.signable_dict())
    pub = client.post("/api/groups/registry/publish", json={"record": rec.to_dict()})
    assert pub.status_code == 200, pub.text

    code = code_for_pubkey(founder.pubkey_hex)
    res = client.get(f"/api/agents/by_code/{code}", params={"actor_id": "admin"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "group"
    assert body["pubkey_hex"] == founder.pubkey_hex


def test_message_triggers_echo_reply_on_home(client):
    """End-to-end demo: send a message, the echo agent replies in the channel."""
    res = client.post("/api/messages", json={
        "agent_id": "admin",
        "channel_id": "general",
        "body": "hi from admin",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    # The user's own message is the primary return value
    assert body["sender_id"] == "admin"
    # The echo agent piggy-backed a reply
    assert "echo_reply" in body
    assert body["echo_reply"]["sender_id"] == DEFAULT_AGENT_ID
    assert "hi from admin" in body["echo_reply"]["body"]

    # And the reply landed in the channel — the next list_messages sees both.
    state = client.get("/api/state", params={"agent_id": "admin"}).json()
    bodies = [m["body"] for m in state["messages"]]
    assert any(b == "hi from admin" for b in bodies)
    assert any("EchoAgent" in b for b in bodies)


def test_echo_does_not_loop_on_its_own_replies(client):
    """The responder must not respond to its own messages."""
    # Send one message; this should produce exactly one echo reply.
    client.post("/api/messages", json={
        "agent_id": "admin",
        "channel_id": "general",
        "body": "round 1",
    })
    # After several polls, we should still have only 2 messages, not a runaway.
    state = client.get("/api/state", params={"agent_id": "admin"}).json()
    assert len(state["messages"]) == 2
