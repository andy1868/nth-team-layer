"""P4: agent collaboration primitives reachable via facade + attach.

Original review finding: the 4 new modules were testable via direct
import but NOT presented as user-facing API. Production users
following the README would have no idea ActionRouter / FaultIsolator
exist, let alone how to wire them together.

This suite checks the entry points exist + work + share state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nth_dao
from nth_dao.action_routing import ActionRouter
from nth_dao.agent_profile import AgentProfile
from nth_dao.event_subscriptions import SubscriptionManager
from nth_dao.fault_isolation import FaultIsolator


# ===== nth_dao facade =====


def test_P4_facade_exposes_collab_primitives():
    """The 4 new modules + their key types reachable as nth_dao.<X>."""
    for name in (
        "AgentProfile",
        "ActionRouter", "ActionRequest", "ActionResponse",
        "ActionStatus", "RouteStrategy",
        "SubscriptionManager", "Subscription",
        "FaultIsolator", "CircuitState", "AgentHealth",
    ):
        assert hasattr(nth_dao, name), f"missing facade export: {name}"


def test_P4_facade_types_match_module_types():
    assert nth_dao.AgentProfile is AgentProfile
    assert nth_dao.ActionRouter is ActionRouter
    assert nth_dao.SubscriptionManager is SubscriptionManager
    assert nth_dao.FaultIsolator is FaultIsolator


# ===== TeamSession lazy accessors =====


def _make_session(tmp_path: Path):
    """Build a minimal TeamSession - we only test the new accessors,
    not the rest of attach()."""
    import nth_dao as nth
    return nth.attach(
        agent_id="alice",
        backend=None,
        capabilities=["chat"],
        groups=[],
        workspace=tmp_path,
        start_heartbeat=False,
    )


def test_P4_session_event_bus_lazy_and_cached(tmp_path: Path):
    sess = _make_session(tmp_path)
    bus1 = sess.event_bus()
    bus2 = sess.event_bus()
    assert bus1 is bus2   # same instance returned


def test_P4_session_subscriptions_share_event_bus(tmp_path: Path):
    sess = _make_session(tmp_path)
    subs = sess.subscriptions()
    # Subscriptions wraps the same bus the session uses
    assert subs._bus is sess.event_bus()


def test_P4_session_fault_isolator_wired_to_event_bus(tmp_path: Path):
    sess = _make_session(tmp_path)
    iso = sess.fault_isolator()
    assert iso._event_bus is sess.event_bus()


def test_P4_session_action_router_fails_closed_without_signing_identity(tmp_path: Path):
    """Without a signing identity, action_router must not silently
    downgrade to unsigned dev mode."""
    sess = _make_session(tmp_path)
    with pytest.raises(ValueError, match="allow_unsigned_dev=True"):
        sess.action_router()


def test_P4_session_action_router_dev_mode_is_explicit(tmp_path: Path):
    sess = _make_session(tmp_path)
    router = sess.action_router(allow_unsigned_dev=True)
    assert router._verify_enabled is False


def test_P4_session_action_router_prod_mode_with_signing_identity(tmp_path: Path):
    from nth_dao.identity import AgentIdentity, crypto_available
    if not crypto_available():
        pytest.skip("PyNaCl required")
    sess = _make_session(tmp_path)
    sess.identity = AgentIdentity.generate(label="alice")
    router = sess.action_router()
    assert router._verify_enabled is True


def test_P4_session_profile_aggregates_known_data(tmp_path: Path):
    sess = _make_session(tmp_path)
    profile = sess.profile()
    assert profile.agent_id == "alice"
    # capabilities pulled from the registry record attach() created
    assert "chat" in profile.capabilities


def test_P4_end_to_end_failure_event_visible_via_subscription(tmp_path: Path):
    """Smoke regression for the whole P4 wiring: a fault recorded by
    the FaultIsolator must reach a SubscriptionManager listener over
    the shared EventBus."""
    sess = _make_session(tmp_path)
    iso = sess.fault_isolator()
    subs = sess.subscriptions()

    received: list = []
    subs.subscribe("failure.observed", lambda ev: received.append(ev.payload))

    iso.record_failure("bob", action_type="x", error="boom")
    iso.flush()
    subs.poll()

    assert len(received) == 1
    assert received[0]["agent_id"] == "bob"
