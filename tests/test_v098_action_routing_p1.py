"""P1: explicit dev mode contract.

Production-grade security primitive should NEVER default to "accept
everything unsigned". The original code silently degraded to dev mode
when either identity or pubkey_lookup was missing; integrators who
forgot to wire the directory would ship an open router.

Now: construction REFUSES unless both prerequisites are present, OR
allow_unsigned_dev=True is set explicitly (so reviewers and config
files surface the dev-mode decision).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nth_dao.action_routing import ActionRequest, ActionRouter, ActionStatus
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


# ===== constructor refuses incomplete production setup =====


def test_P1_refuses_with_no_identity_no_lookup(tmp_path: Path):
    with pytest.raises(ValueError, match="identity"):
        ActionRouter(agent_id="alice", workspace=tmp_path)


def test_P1_refuses_with_identity_but_no_lookup(tmp_path: Path, alice):
    with pytest.raises(ValueError, match="pubkey_lookup"):
        ActionRouter(agent_id="alice", identity=alice, workspace=tmp_path)


def test_P1_refuses_with_lookup_but_no_identity(tmp_path: Path):
    with pytest.raises(ValueError, match="identity"):
        ActionRouter(
            agent_id="alice",
            pubkey_lookup=lambda aid: None,
            workspace=tmp_path,
        )


def test_P1_refuses_with_unsigning_identity(tmp_path: Path):
    """An AgentIdentity without can_sign (e.g. loaded from did:key
    only) doesn't count - it can verify but it can't sign responses,
    so a router built with it would emit unsigned ResponseSig and
    break the trust loop."""
    class UnsigningIdentity:
        can_sign = False
        agent_id = "x"
    with pytest.raises(ValueError, match="can_sign=True"):
        ActionRouter(
            agent_id="alice",
            identity=UnsigningIdentity(),  # type: ignore[arg-type]
            pubkey_lookup=lambda aid: None,
            workspace=tmp_path,
        )


def test_P1_error_mentions_dev_mode_escape_hatch(tmp_path: Path):
    """The error message should TELL the integrator how to get out of
    this in a dev / notebook context, so the fix isn't a desperate
    search through docs."""
    with pytest.raises(ValueError, match="allow_unsigned_dev=True"):
        ActionRouter(agent_id="alice", workspace=tmp_path)


# ===== explicit dev mode works =====


def test_P1_explicit_dev_mode_accepts_everything(tmp_path: Path):
    router = ActionRouter(
        agent_id="alice", workspace=tmp_path,
        allow_unsigned_dev=True,
    )
    router.register("ping", lambda r: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice",
    )
    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


def test_P1_production_mode_works_normally(tmp_path: Path, alice):
    """Sanity: with both prereqs present, no need for dev opt-in."""
    router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=lambda aid: alice.pubkey_hex if aid == "alice" else None,
        workspace=tmp_path,
    )
    router.register("ping", lambda r: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="alice", to_agent="alice",
    )
    req.sig = alice.sign_json(req.signable_dict())
    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value
