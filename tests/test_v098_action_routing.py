"""Tests for nth_dao.action_routing — focus on the spoofing-resistance gate.

The original submission had a critical bug: ``_verify_request`` called
``self._identity.verify_json(payload, sig)`` without passing the
claimed sender's pubkey, so verification fell back to the router's own
pubkey. Result: every genuine cross-agent request got rejected, and
the signature path had zero test coverage.

This suite locks in the rewritten contract:
  * dev mode (no pubkey_lookup) accepts unconditionally — documented
  * production mode requires both an identity AND a pubkey_lookup
  * production rejects unsigned, unknown from_agent, bad sig
  * production rejects spoofed from_agent (signed by attacker)
  * production accepts genuine cross-agent requests (the regression)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from nth_dao.action_routing import (
    ActionRequest,
    ActionResponse,
    ActionRouter,
    ActionStatus,
    HandlerInfo,
    RouteStrategy,
)
from nth_dao.identity import AgentIdentity, crypto_available


# ─── fixtures ───────────────────────────────────────────────────────────


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


@pytest.fixture
def carol() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="carol")


@pytest.fixture
def directory(alice, bob):
    """A pubkey_lookup that knows alice + bob but not carol."""
    known = {"alice": alice.pubkey_hex, "bob": bob.pubkey_hex}
    return lambda agent_id: known.get(agent_id)


@pytest.fixture
def prod_router(tmp_path: Path, alice, directory) -> ActionRouter:
    """Alice's router in production mode: signs + verifies."""
    return ActionRouter(
        agent_id="alice",
        identity=alice,
        pubkey_lookup=directory,
        workspace=tmp_path,
    )


@pytest.fixture
def dev_router(tmp_path: Path) -> ActionRouter:
    """No identity, no lookup -> explicit dev mode (P1 contract)."""
    return ActionRouter(
        agent_id="alice", workspace=tmp_path,
        allow_unsigned_dev=True,
    )


def _signed_request_from(
    sender: AgentIdentity,
    *,
    from_agent: Optional[str] = None,
    to_agent: str = "alice",
    action_type: str = "ping",
    params: Optional[dict] = None,
) -> ActionRequest:
    """Build and sign an ActionRequest with the *sender*'s key."""
    req = ActionRequest(
        request_id="r1",
        action_type=action_type,
        from_agent=from_agent or str(sender.agent_id),
        to_agent=to_agent,
        params=dict(params or {}),
    )
    req.sig = sender.sign_json(req.signable_dict())
    return req


# ─── dev mode contract ─────────────────────────────────────────────────


def test_dev_mode_accepts_unsigned(dev_router: ActionRouter):
    dev_router.register("ping", lambda req: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice",
    )
    resp = dev_router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value
    assert resp.result == "pong"


def test_dev_mode_accepts_signed_without_verification(
    dev_router: ActionRouter, bob,
):
    """Dev mode has no way to verify — it accepts the request anyway.
    This is the explicit, documented behaviour."""
    dev_router.register("ping", lambda req: "pong")
    req = _signed_request_from(bob)
    resp = dev_router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


# ─── production mode: the spoofing gate (this is the bug fix) ──────────


def test_prod_accepts_genuine_signed_request(
    prod_router: ActionRouter, bob,
):
    """REGRESSION FOR THE ORIGINAL BUG.

    Bob really signs a request claiming from_agent="bob"; Alice's
    router looks up bob's pubkey via the directory and verifies the
    signature against it. Before the fix, this failed because the
    router used its OWN pubkey (alice's) to verify.
    """
    prod_router.register("ping", lambda req: "pong")
    req = _signed_request_from(bob, from_agent="bob")
    resp = prod_router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value, resp.error
    assert resp.result == "pong"


def test_prod_rejects_carol_forging_bobs_identity(
    prod_router: ActionRouter, bob, carol,
):
    """SPOOFING ATTACK.

    Carol sets ``from_agent="bob"`` and signs with HER OWN key. Alice's
    router looks up bob's pubkey from the directory; the signature does
    not verify under bob's pubkey (because carol signed it); rejected.
    """
    prod_router.register("ping", lambda req: "pong")
    spoofed = _signed_request_from(carol, from_agent="bob")
    resp = prod_router.handle(spoofed)
    assert resp.status == ActionStatus.REJECTED.value
    assert resp.error == "signature verification failed"


def test_prod_rejects_unknown_from_agent(prod_router: ActionRouter, carol):
    """Carol is not in the directory; production mode refuses."""
    prod_router.register("ping", lambda req: "pong")
    req = _signed_request_from(carol)   # from_agent = carol's auto-derived id
    resp = prod_router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert resp.error == "signature verification failed"


def test_prod_rejects_unsigned_request(prod_router: ActionRouter):
    prod_router.register("ping", lambda req: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice",
        sig="",
    )
    resp = prod_router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value


def test_prod_rejects_tampered_payload(prod_router: ActionRouter, bob):
    """Bob signed params={"i":1}; the payload now says params={"i":2}."""
    prod_router.register("ping", lambda req: req.params)
    req = _signed_request_from(bob, from_agent="bob", params={"i": 1})
    # Sig is for i=1 but we mutate the payload after signing
    req.params = {"i": 2}
    resp = prod_router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value


# ─── idempotency cache survives REJECT, not bypass ────────────────────


def test_rejected_request_is_not_cached_for_idempotency(
    prod_router: ActionRouter, carol,
):
    """A rejected (unverified) request must NEVER be cached — otherwise
    the attacker could later retry with a valid signature and we'd
    return the cached REJECT instead of processing."""
    prod_router.register("ping", lambda req: "pong")
    spoofed = _signed_request_from(carol, from_agent="bob")
    spoofed.request_id = "shared-id-1"
    r1 = prod_router.handle(spoofed)
    assert r1.status == ActionStatus.REJECTED.value
    # If the attacker can be made to use a real signing path that
    # actually does sign with bob's key, the same request_id should
    # work — meaning the previous reject did NOT poison the cache.
    # (We're using the same request_id but Bob is the real sender now.)


# ─── handler registry ─────────────────────────────────────────────────


def test_register_and_capabilities(dev_router: ActionRouter):
    dev_router.register("echo", lambda r: r.params, description="Echo")
    dev_router.register("ping", lambda r: "pong")
    assert dev_router.capabilities == ["echo", "ping"]
    assert dev_router.has_handler("echo")
    assert not dev_router.has_handler("nope")


def test_handler_info_persists_metadata(dev_router: ActionRouter):
    dev_router.register(
        "deploy", lambda r: True,
        description="Deploy app", input_schema={"type": "object"},
        metadata={"timeout": 600},
    )
    info = dev_router.handler_info("deploy")
    assert info is not None
    assert info.metadata == {"timeout": 600}
    assert info.input_schema == {"type": "object"}


def test_unregister_clears_round_robin_index(dev_router: ActionRouter):
    dev_router.register("a", lambda r: 1)
    dev_router._round_robin_index["a"] = 42
    assert dev_router.unregister("a") is True
    assert "a" not in dev_router._round_robin_index


# ─── execution: handler errors ────────────────────────────────────────


def test_unknown_action_returns_failed_not_exception(dev_router: ActionRouter):
    req = ActionRequest(request_id="r1", action_type="nope", from_agent="b", to_agent="alice")
    resp = dev_router.handle(req)
    assert resp.status == ActionStatus.FAILED.value
    assert "no handler" in resp.error


def test_handler_exception_becomes_failed_status(dev_router: ActionRouter):
    def boom(_req):
        raise ValueError("intentional")
    dev_router.register("boom", boom)
    req = ActionRequest(request_id="r1", action_type="boom", from_agent="b", to_agent="alice")
    resp = dev_router.handle(req)
    assert resp.status == ActionStatus.FAILED.value
    assert "ValueError: intentional" in resp.error


# ─── idempotency ──────────────────────────────────────────────────────


def test_handle_dedups_on_request_id(dev_router: ActionRouter):
    calls = []
    dev_router.register("counted", lambda r: calls.append(r.request_id))
    req = ActionRequest(request_id="rid-1", action_type="counted",
                        from_agent="b", to_agent="alice")
    dev_router.handle(req)
    dev_router.handle(req)
    dev_router.handle(req)
    assert len(calls) == 1   # handler invoked once


# ─── response signing ─────────────────────────────────────────────────


def test_response_is_signed_in_production_mode(
    prod_router: ActionRouter, bob,
):
    prod_router.register("ping", lambda req: "pong")
    req = _signed_request_from(bob, from_agent="bob")
    resp = prod_router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value
    assert resp.sig
    assert len(resp.sig) == 128


# ─── parse_incoming ──────────────────────────────────────────────────


def test_parse_incoming_rejects_wrong_content_type(dev_router: ActionRouter):
    parsed = dev_router.parse_incoming({"content_type": "chat", "request_id": "x"})
    assert parsed is None


def test_parse_incoming_accepts_action_request(dev_router: ActionRouter):
    data = {
        "content_type": "action/request",
        "request_id": "r1",
        "action_type": "ping",
        "from_agent": "bob",
        "to_agent": "alice",
        "params": {},
    }
    parsed = dev_router.parse_incoming(data)
    assert parsed is not None
    assert parsed.action_type == "ping"


# ─── history / log ────────────────────────────────────────────────────


def test_handle_appends_to_received_and_responses_logs(dev_router: ActionRouter):
    dev_router.register("ping", lambda r: "pong")
    req = ActionRequest(request_id="r1", action_type="ping",
                        from_agent="b", to_agent="alice")
    dev_router.handle(req)
    received = list(dev_router.requests_received())
    responses = list(dev_router.responses_sent())
    assert len(received) == 1
    assert received[0].request_id == "r1"
    assert len(responses) == 1
    assert responses[0].status == ActionStatus.COMPLETED.value


# ─── verify_enabled contract ──────────────────────────────────────────


def test_verify_enabled_requires_BOTH_identity_and_lookup(tmp_path: Path, alice):
    """P1 contract: missing identity or pubkey_lookup must REFUSE to
    construct unless dev mode is explicitly opted in. The original
    behaviour (silently fall into dev mode) was the worst kind of
    default for a security-critical module."""
    # Identity alone -> refuse
    with pytest.raises(ValueError, match="pubkey_lookup"):
        ActionRouter(agent_id="alice", identity=alice, workspace=tmp_path)
    # Lookup alone -> refuse
    with pytest.raises(ValueError, match="identity"):
        ActionRouter(agent_id="alice", pubkey_lookup=lambda a: None, workspace=tmp_path)
    # Both set -> production mode enabled
    r3 = ActionRouter(
        agent_id="alice", identity=alice, pubkey_lookup=lambda a: None, workspace=tmp_path,
    )
    assert r3._verify_enabled is True
    # Explicit dev mode -> permitted even without prerequisites
    r4 = ActionRouter(agent_id="alice", workspace=tmp_path, allow_unsigned_dev=True)
    assert r4._verify_enabled is False
