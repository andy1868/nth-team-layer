"""P0 critical fixes for action_routing:

P0-#1 parse_incoming must handle the real ChannelMessage envelope shape
      produced by route() -> channel.send(), not just the flat-dict
      shape that the original unit tests used.

P0-#2 handle() must reject requests addressed to other agents BEFORE
      executing the handler. A signed request for Alice that lands at
      Bob's router must NOT trigger Bob's handlers.

End-to-end regression: route -> channel deliver -> parse_incoming
      -> handle. Without these fixes the production loop was broken;
      every existing test sidestepped this path by hand-building the
      ActionRequest dataclass.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import pytest

from nth_dao.action_routing import (
    MAX_LOG_TO_AGENT_LEN,
    ActionRequest,
    ActionResponse,
    ActionRouter,
    ActionStatus,
)
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bob() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob")


# ===== P0-#1: parse_incoming with real ChannelMessage envelope =====


def test_P0_1_parse_real_channel_envelope_with_content_json(tmp_path: Path):
    """A ChannelMessage as produced by route() has the request payload
    as a JSON STRING under `content`. parse_incoming must JSON-decode
    that string, not try to read fields from the envelope top-level."""
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    inner = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice",
        params={"x": 1},
    )
    envelope = {
        "msg_id": "abc123",
        "channel": "dm:somehash",
        "from_agent": "bob",
        "content": json.dumps(inner.to_dict()),
        "content_type": "action/request",
        "metadata": {
            "action_type": "ping",
            "request_id": "r1",
        },
    }
    parsed = router.parse_incoming(envelope)
    assert parsed is not None
    assert parsed.request_id == "r1"
    assert parsed.action_type == "ping"
    assert parsed.from_agent == "bob"
    assert parsed.to_agent == "alice"
    assert parsed.params == {"x": 1}


def test_P0_1_parse_still_accepts_flat_dict_for_back_compat(tmp_path: Path):
    """Old callers that pass a flat dict (the way every unit test in this
    suite used to) must still work — back-compat."""
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    flat = {
        "content_type": "action/request",
        "request_id": "r1",
        "action_type": "ping",
        "from_agent": "bob",
        "to_agent": "alice",
        "params": {},
    }
    parsed = router.parse_incoming(flat)
    assert parsed is not None
    assert parsed.action_type == "ping"


def test_P0_1_parse_rejects_envelope_with_invalid_json_content(tmp_path: Path):
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    bad = {
        "content_type": "action/request",
        "content": "{not valid json",
    }
    assert router.parse_incoming(bad) is None


def test_P0_1_parse_rejects_envelope_with_non_object_content(tmp_path: Path):
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    bad = {
        "content_type": "action/request",
        "content": "[\"this is a list, not an object\"]",
    }
    assert router.parse_incoming(bad) is None


def test_P0_1_envelope_sender_mismatch_logs_but_does_not_reject(
    tmp_path: Path, caplog,
):
    """A relay agent (B) might forward a request originally from A. The
    envelope's from_agent is B but the inner request's from_agent is A.
    This is legitimate; signature verification carries the real auth.
    Log it (so a security team can spot anomalies) but don't reject."""
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    inner = {
        "request_id": "r1", "action_type": "ping",
        "from_agent": "actual-sender",
        "to_agent": "alice",
    }
    envelope = {
        "content_type": "action/request",
        "content": json.dumps(inner),
        "from_agent": "intermediate-relay",   # different
    }
    parsed = router.parse_incoming(envelope)
    assert parsed is not None
    assert parsed.from_agent == "actual-sender"   # honoured


# ===== P0-#2: handle() target gate =====


def test_P0_2_handle_rejects_misdirected_signed_request(tmp_path: Path, bob):
    """Bob signs a request for Carol; it ends up at Alice's router.
    Signature is valid. Without the target gate, Alice executes Carol's
    handlers."""
    router = ActionRouter(
        agent_id="alice",
        identity=bob,    # to enable signing/verify; doesn't matter who
        pubkey_lookup=lambda aid: bob.pubkey_hex if aid == "bob" else None,
        workspace=tmp_path,
    )
    called = []
    router.register("ping", lambda r: called.append(r))

    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="carol",   # NOT alice
    )
    req.sig = bob.sign_json(req.signable_dict())

    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert "misdirected" in resp.error
    assert called == [], "handler must NOT execute for misdirected request"
    assert resp.sig == "", "rejected response must not be signed"


def test_P0_2_handle_accepts_correctly_addressed(tmp_path: Path, alice, bob):
    """Same router setup but request is correctly to_agent=alice."""
    router = ActionRouter(
        agent_id="alice",
        identity=alice,
        pubkey_lookup=lambda aid: bob.pubkey_hex if aid == "bob" else None,
        workspace=tmp_path,
    )
    router.register("ping", lambda r: "pong")

    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice",
    )
    req.sig = bob.sign_json(req.signable_dict())

    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value
    assert resp.result == "pong"


def test_P0_2_handle_accepts_empty_to_agent_back_compat(tmp_path: Path):
    """A request with empty to_agent (e.g. dev calls that don't set it)
    is still accepted - we only reject when to_agent is set AND wrong.
    Otherwise we'd break every smoke test that builds ActionRequest()."""
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    router.register("ping", lambda r: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="",
    )
    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


# ===== End-to-end: route -> envelope -> parse -> handle =====


class FakeChannel:
    """A minimal in-process TeamChannel substitute. Records each send;
    delivers verbatim. Mirrors the real ChannelMessage envelope shape."""
    def __init__(self):
        self.sent: list = []

    def send(self, *, content, scope, content_type, metadata):
        envelope = {
            "msg_id": f"m{len(self.sent)}",
            "channel": scope,
            "from_agent": "bob",   # router fills this in real life
            "content": content,
            "content_type": content_type,
            "metadata": metadata,
        }
        self.sent.append(envelope)
        return envelope


def test_END_TO_END_route_to_handle_via_envelope(tmp_path: Path, alice, bob):
    """The single test the original suite was missing: drive the FULL
    production loop. Bob's router calls route() -> envelope hits the
    channel -> Alice's router calls handle_incoming(envelope) -> the
    handler runs once.

    With P0-#1 broken, parse_incoming would return None and the handler
    never fires. With P0-#2 broken, Carol's router would also fire.
    """
    channel = FakeChannel()
    bob_router = ActionRouter(
        agent_id="bob", identity=bob, channel=channel,  # type: ignore[arg-type]
        workspace=tmp_path / "bob",
    )
    alice_router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=lambda aid: bob.pubkey_hex if aid == "bob" else None,
        workspace=tmp_path / "alice",
    )
    invocations: list = []
    alice_router.register("ping", lambda r: invocations.append(r.request_id))

    # Bob routes
    bob_router.route("ping", {"x": 1}, target_agent="alice")
    assert len(channel.sent) == 1
    envelope = channel.sent[0]

    # Alice processes the envelope
    resp = alice_router.handle_incoming(envelope)
    assert resp is not None
    assert resp.status == ActionStatus.COMPLETED.value
    assert len(invocations) == 1
    assert invocations[0] == envelope["metadata"]["request_id"]


def test_END_TO_END_carol_does_not_execute_alice_bound_request(
    tmp_path: Path, alice, bob,
):
    """Multi-router fan-out scenario: the same envelope reaches Carol
    by mistake (broadcast misroute). Alice executes once, Carol rejects."""
    channel = FakeChannel()
    bob_router = ActionRouter(
        agent_id="bob", identity=bob, channel=channel,  # type: ignore[arg-type]
        workspace=tmp_path / "bob",
    )

    def lookup(aid):
        return bob.pubkey_hex if aid == "bob" else None

    alice_router = ActionRouter(
        agent_id="alice", identity=alice, pubkey_lookup=lookup,
        workspace=tmp_path / "alice",
    )
    carol_router = ActionRouter(
        agent_id="carol",
        identity=AgentIdentity.generate(label="carol"),
        pubkey_lookup=lookup,
        workspace=tmp_path / "carol",
    )
    alice_calls: list = []
    carol_calls: list = []
    alice_router.register("ping", lambda r: alice_calls.append(1))
    carol_router.register("ping", lambda r: carol_calls.append(1))

    bob_router.route("ping", {}, target_agent="alice")
    envelope = channel.sent[0]

    alice_router.handle_incoming(envelope)
    carol_router.handle_incoming(envelope)

    assert alice_calls == [1]   # correctly executed
    assert carol_calls == []    # rejected: to_agent='alice' != self.agent_id='carol'
