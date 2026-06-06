"""V-34 follow-up: emit_settlement_completed audit-chain integrity.

The first Voss V-34 fix made the three Mandate emit helpers refuse
to emit for unsigned / invalidly-signed mandates. But
``emit_settlement_completed`` was deferred to backlog because the
chain check requires reading prior events off the EventBus.

This file pins the deferred work:

  V-34a settlement.completed refused when no prior
        mandate.payment.authorised carries the same digest
  V-34b settlement.completed allowed when the prior chain exists
  V-34c require_prior_authorisation=False opt-out works (for
        migration scenarios; production must NEVER use it)
  V-34d the chain check accepts ANY prior authorisation regardless
        of which actor emitted it (the digest IS the binding)
  V-34e multiple authorisations on the bus don't confuse the check
  V-34f the chain check fires AFTER input validation (so input
        errors still produce their canonical messages)
  V-34g a fresh empty bus rejects settlement.completed
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from nth_dao.event_bus import EventBus, MANDATE_PAYMENT_AUTHORISED
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    sign_cart_mandate,
)
from nth_dao.mandate.events import (
    emit_payment_authorised,
    emit_settlement_completed,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    payment_mandate_digest,
    sign_payment_mandate,
)


# ----- fixtures -----


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t14-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t14-seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t14-agent").as_did()


@pytest.fixture
def bus(tmp_path: Path, dao) -> EventBus:
    return EventBus(tmp_path, identity=dao)


def _future(s: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()


def _signed_chain(dao, seller, agent_did) -> Dict[str, Any]:
    """Build the full signed Intent->Cart->Payment chain. Returns
    a dict of the three mandates so tests can pick whichever."""
    intent = sign_intent_mandate(build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did, purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    ), dao)
    cart = sign_cart_mandate(build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(intent),
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    ), seller)
    payment = sign_payment_mandate(build_payment_mandate(
        issuer_did=dao.as_did(), payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc", expires_at=_future(900),
    ), dao)
    return {"intent": intent, "cart": cart, "payment": payment}


# =====================================================================
# V-34a: refused when no prior authorisation
# =====================================================================


def test_T14_V34a_settlement_refused_without_prior_authorisation(
    bus, dao, seller, agent_did,
):
    """A SettlementAdapter that tries to emit settlement.completed
    for a digest that was NEVER announced as authorised must be
    refused. This is the core audit-chain integrity check."""
    chain = _signed_chain(dao, seller, agent_did)
    # NOTE: we deliberately do NOT call emit_payment_authorised
    digest = payment_mandate_digest(chain["payment"])

    with pytest.raises(ValueError, match="no prior mandate.payment.authorised"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex=digest,
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="success",
        )


def test_T14_V34a_empty_bus_rejects_settlement(bus):
    """A completely empty EventBus must reject settlement.completed
    for any digest - there's no prior anything."""
    with pytest.raises(ValueError, match="no prior mandate.payment.authorised"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="success",
        )


def test_T14_V34a_refused_for_unrelated_digest(bus, dao, seller, agent_did):
    """A bus that has SOME payment.authorised events but none with
    the requested digest must reject. Otherwise the check could be
    bypassed by emitting any prior authorisation."""
    chain = _signed_chain(dao, seller, agent_did)
    emit_payment_authorised(bus, chain["payment"])

    # Now try to complete settlement for a DIFFERENT digest
    other_digest = "b" * 64
    with pytest.raises(ValueError, match="no prior mandate.payment.authorised"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex=other_digest,
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="success",
        )


# =====================================================================
# V-34b: allowed when prior chain exists
# =====================================================================


def test_T14_V34b_settlement_allowed_after_authorisation(
    bus, dao, seller, agent_did,
):
    chain = _signed_chain(dao, seller, agent_did)
    emit_payment_authorised(bus, chain["payment"])
    digest = payment_mandate_digest(chain["payment"])

    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
        receipt={"tx_hash": "0xabc"},
    )
    assert ev.event_type == "settlement.completed"
    assert ev.payload["payment_mandate_digest"] == digest


def test_T14_V34b_allowed_for_failure_outcome_too(bus, dao, seller, agent_did):
    """The chain check doesn't care whether the settlement succeeded
    or failed - either is a legitimate audit event as long as the
    payment was authorised."""
    chain = _signed_chain(dao, seller, agent_did)
    emit_payment_authorised(bus, chain["payment"])
    digest = payment_mandate_digest(chain["payment"])

    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="failure",
        receipt={"error": "insufficient_funds"},
    )
    assert ev.payload["outcome"] == "failure"


# =====================================================================
# V-34c: opt-out via require_prior_authorisation=False
# =====================================================================


def test_T14_V34c_opt_out_allows_settlement_without_chain(bus):
    """For migration scenarios (importing settlement history from a
    legacy system), the opt-out flag bypasses the chain check.
    Production must NEVER pass False - that's enforced only by
    documentation, not runtime, which is acceptable since this
    is a backstop guard, not an authn boundary."""
    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex="a" * 64,
        adapter="legacy",
        settlement_choice="legacy:migration",
        outcome="success",
        require_prior_authorisation=False,
    )
    assert ev.event_type == "settlement.completed"


# =====================================================================
# V-34d: accept ANY prior authorisation regardless of actor identity
# =====================================================================


def test_T14_V34d_chain_check_actor_agnostic(
    tmp_path, dao, seller, agent_did,
):
    """The digest is the binding; the chain check must accept a prior
    payment.authorised even if it was emitted by a DIFFERENT actor.
    This is by design: the SettlementAdapter that completes settlement
    is normally a separate process from the agent that authorised the
    payment, and they have different identities."""
    chain = _signed_chain(dao, seller, agent_did)

    # Bus_one: dao emits authorisation
    bus_one_path = tmp_path / "bus_one"
    bus_one_path.mkdir()
    bus_one = EventBus(bus_one_path, identity=dao)
    emit_payment_authorised(bus_one, chain["payment"])

    # Bus_one: a DIFFERENT actor (the settlement adapter) emits completion
    settlement_adapter = AgentIdentity.generate(label="t14-adapter")
    digest = payment_mandate_digest(chain["payment"])
    ev = emit_settlement_completed(
        bus_one,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
        identity=settlement_adapter,
    )
    assert ev.event_type == "settlement.completed"


# =====================================================================
# V-34e: multiple authorisations on the bus don't confuse the check
# =====================================================================


def test_T14_V34e_chain_check_picks_correct_among_many(
    bus, dao, seller, agent_did,
):
    """A bus with N different payment.authorised events must allow
    settlement for any one of them, and reject for none."""
    # Build three independent payment chains
    chains = [_signed_chain(dao, seller, agent_did) for _ in range(3)]
    digests = []
    for c in chains:
        emit_payment_authorised(bus, c["payment"])
        digests.append(payment_mandate_digest(c["payment"]))

    # All three digests can settle independently
    for digest in digests:
        ev = emit_settlement_completed(
            bus,
            payment_mandate_digest_hex=digest,
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="success",
        )
        assert ev.payload["payment_mandate_digest"] == digest


# =====================================================================
# V-34f: input validation runs BEFORE chain check
# =====================================================================


def test_T14_V34f_input_errors_still_produce_canonical_messages(bus):
    """Tampering the digest into something malformed should produce
    the input-validation error message, not the chain-check error.
    The order of checks matters for caller-facing diagnostics:
    input validation is faster and more specific."""
    with pytest.raises(ValueError, match="64-hex"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="too-short",
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="success",
        )
    with pytest.raises(ValueError, match="outcome"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="x402",
            settlement_choice="x402:usdc",
            outcome="maybe",
        )


# =====================================================================
# V-34g: chain integrity in the replayable audit stream
# =====================================================================


def test_T14_V34g_replay_chain_includes_both_events(
    bus, dao, seller, agent_did,
):
    """End-to-end: after a proper authorise+complete pair, replay
    yields both events in stream order. This validates that the
    chain check doesn't suppress legitimate emits."""
    chain = _signed_chain(dao, seller, agent_did)
    ev_auth = emit_payment_authorised(bus, chain["payment"])
    digest = payment_mandate_digest(chain["payment"])
    ev_settle = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
    )

    # Both events should be on the bus
    types = [
        e.event_type for e in bus.replay(event_types=[
            "mandate.payment.authorised", "settlement.completed",
        ])
    ]
    assert types == ["mandate.payment.authorised", "settlement.completed"]


# =====================================================================
# V-34g extra: reverse-order scan optimisation (settlements usually
# follow soon after authorisation, so reverse scan finds the match
# quickly). This pins the optimisation behaviour - if a future
# refactor switches to forward scan and a long event log makes
# perf regress, the docstring's perf claim moves with it.
# =====================================================================


def test_T14_V34g_chain_check_finds_match_after_many_unrelated_events(
    bus, dao, seller, agent_did,
):
    """Sanity: even when many UNRELATED events follow the auth event,
    settlement.completed for the original digest still works."""
    chain = _signed_chain(dao, seller, agent_did)
    emit_payment_authorised(bus, chain["payment"])
    digest = payment_mandate_digest(chain["payment"])

    # Pad the bus with unrelated events
    for i in range(20):
        bus.emit("unrelated.thing", {"i": i})

    # The chain check must still find the auth event
    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
    )
    assert ev.event_type == "settlement.completed"
