"""T-5: Mandate-lifecycle EventBus event types reserved + emit helpers.

Four canonical event_type strings are reserved on the EventBus:

    mandate.intent.issued       (after IntentMandate signed)
    mandate.cart.received       (after CartMandate received from counterparty)
    mandate.payment.authorised  (after DAO signs PaymentMandate)
    settlement.completed        (after SettlementAdapter finishes)

Subsystems MUST use these via the emit_* helpers - the helpers
guarantee canonical payload shapes so downstream consumers
(dashboards, settlement adapters, third-party watchers) can filter
and decode without surprise.

Tests cover:
  * Constants exposed on event_bus + nth_dao facade
  * Each helper emits with correct event_type
  * Each helper extracts the right payload fields from a Mandate dict
  * Each helper rejects malformed input (catches subsystem bugs early)
  * Replay across MANDATE_LIFECYCLE_EVENT_TYPES returns the full
    audit chain in order
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nth_dao.event_bus import (
    MANDATE_CART_RECEIVED,
    MANDATE_INTENT_ISSUED,
    MANDATE_LIFECYCLE_EVENT_TYPES,
    MANDATE_PAYMENT_AUTHORISED,
    SETTLEMENT_COMPLETED,
    EventBus,
)
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
)
from nth_dao.mandate.events import (
    emit_cart_received,
    emit_intent_issued,
    emit_payment_authorised,
    emit_settlement_completed,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    payment_mandate_digest,
)


# ===== fixtures =====


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob").as_did()


@pytest.fixture
def bus(tmp_path: Path, dao) -> EventBus:
    return EventBus(tmp_path, identity=dao)


def _future(s=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()


def _make_intent(dao, agent_did, **overrides):
    return build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
        **overrides,
    )


def _make_cart(seller, agent_did, intent_digest):
    return build_cart_mandate(
        issuer_did=seller.as_did(),
        buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "Code review of PR #42", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"],
        expires_at=_future(3600),
    )


def _make_payment(dao, seller, cart_digest):
    return build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice="x402:usdc",
        expires_at=_future(900),
    )


# ===== T5-#1: constants exposed =====


def test_T5_constants_have_canonical_string_values():
    """The exact strings are the wire-format contract. Changing them
    is a breaking protocol change - other implementations match these
    literals."""
    assert MANDATE_INTENT_ISSUED == "mandate.intent.issued"
    assert MANDATE_CART_RECEIVED == "mandate.cart.received"
    assert MANDATE_PAYMENT_AUTHORISED == "mandate.payment.authorised"
    assert SETTLEMENT_COMPLETED == "settlement.completed"
    assert MANDATE_LIFECYCLE_EVENT_TYPES == frozenset({
        "mandate.intent.issued",
        "mandate.cart.received",
        "mandate.payment.authorised",
        "settlement.completed",
    })


def test_T5_facade_exposes_constants_and_helpers():
    """Top-level nth_dao namespace must expose both constants AND
    helpers so integrators using `import nth_dao as nth` get them
    without drilling into event_bus or mandate.events."""
    import nth_dao
    assert nth_dao.MANDATE_INTENT_ISSUED == "mandate.intent.issued"
    assert nth_dao.MANDATE_CART_RECEIVED == "mandate.cart.received"
    assert nth_dao.MANDATE_PAYMENT_AUTHORISED == "mandate.payment.authorised"
    assert nth_dao.SETTLEMENT_COMPLETED == "settlement.completed"
    assert nth_dao.MANDATE_LIFECYCLE_EVENT_TYPES == MANDATE_LIFECYCLE_EVENT_TYPES
    assert nth_dao.emit_intent_issued is emit_intent_issued
    assert nth_dao.emit_cart_received is emit_cart_received
    assert nth_dao.emit_payment_authorised is emit_payment_authorised
    assert nth_dao.emit_settlement_completed is emit_settlement_completed


# ===== T5-#2: each helper emits correct event_type + payload =====


def test_T5_emit_intent_issued_payload_shape(bus, dao, agent_did):
    intent = _make_intent(dao, agent_did)
    ev = emit_intent_issued(bus, intent)
    assert ev.event_type == MANDATE_INTENT_ISSUED
    p = ev.payload
    assert p["intent_id"] == intent["credentialSubject"]["intent_id"]
    assert p["intent_mandate_digest"] == intent_mandate_digest(intent)
    assert p["issuer"] == dao.as_did()
    assert p["agent_did"] == agent_did
    assert p["purpose"] == "buy code review"
    assert p["max_amount"] == {"value": "100.00", "currency": "USDC"}
    assert p["expires_at"] == intent["validUntil"]


def test_T5_emit_cart_received_payload_shape(bus, dao, seller, agent_did):
    intent = _make_intent(dao, agent_did)
    cart = _make_cart(seller, agent_did, intent_mandate_digest(intent))
    ev = emit_cart_received(bus, cart)
    assert ev.event_type == MANDATE_CART_RECEIVED
    p = ev.payload
    assert p["cart_id"] == cart["credentialSubject"]["cart_id"]
    assert p["cart_mandate_digest"] == cart_mandate_digest(cart)
    assert p["intent_mandate_digest"] == intent_mandate_digest(intent)
    assert p["issuer"] == seller.as_did()
    assert p["buyer_did"] == agent_did
    assert p["total"] == {"value": "50.00", "currency": "USDC"}
    assert p["settlement_methods"] == ["x402:usdc"]


def test_T5_emit_payment_authorised_payload_shape(bus, dao, seller, agent_did):
    intent = _make_intent(dao, agent_did)
    cart = _make_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, seller, cart_mandate_digest(cart))
    ev = emit_payment_authorised(bus, payment)
    assert ev.event_type == MANDATE_PAYMENT_AUTHORISED
    p = ev.payload
    assert p["payment_id"] == payment["credentialSubject"]["payment_id"]
    assert p["payment_mandate_digest"] == payment_mandate_digest(payment)
    assert p["cart_mandate_digest"] == cart_mandate_digest(cart)
    assert p["issuer"] == dao.as_did()
    assert p["payee_did"] == seller.as_did()
    assert p["settlement_choice"] == "x402:usdc"


def test_T5_emit_settlement_completed_payload_shape(bus):
    digest = "a" * 64
    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=digest,
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
        receipt={"tx_hash": "0xabc123", "block": 12345},
    )
    assert ev.event_type == SETTLEMENT_COMPLETED
    p = ev.payload
    assert p["payment_mandate_digest"] == digest
    assert p["adapter"] == "x402"
    assert p["settlement_choice"] == "x402:usdc"
    assert p["outcome"] == "success"
    assert p["receipt"] == {"tx_hash": "0xabc123", "block": 12345}
    assert p["completed_at"].endswith("+00:00")


# ===== T5-#3: helpers reject malformed input =====


def test_T5_emit_intent_issued_rejects_missing_fields(bus):
    """Catches subsystem bugs early - the audit trail must not be
    polluted with malformed payloads."""
    with pytest.raises(ValueError, match="missing required fields"):
        emit_intent_issued(bus, {"credentialSubject": {}})
    with pytest.raises(ValueError, match="must be a dict"):
        emit_intent_issued(bus, {"credentialSubject": "not a dict"})


def test_T5_emit_cart_received_rejects_missing_intent_binding(bus):
    """The intent_mandate_digest field is the binding to V1. Forgetting
    it would produce an audit entry that can't be cross-referenced."""
    cart_no_binding = {
        "credentialSubject": {
            "cart_id": "abc",
            "id": "did:key:zXX",
            # intent_mandate_digest MISSING
        },
        "issuer": "did:key:zYY",
    }
    with pytest.raises(ValueError, match="missing required fields"):
        emit_cart_received(bus, cart_no_binding)


def test_T5_emit_payment_authorised_rejects_missing_settlement_choice(bus):
    payment_no_choice = {
        "credentialSubject": {
            "payment_id": "abc",
            "id": "did:key:zXX",
            "cart_mandate_digest": "a" * 64,
            # settlement_choice MISSING
        },
        "issuer": "did:key:zYY",
    }
    with pytest.raises(ValueError, match="missing required fields"):
        emit_payment_authorised(bus, payment_no_choice)


def test_T5_emit_settlement_completed_rejects_bad_inputs(bus):
    """Strict validation on the few enforced contracts (digest hex
    shape, outcome enum, adapter non-empty, settlement_choice format).
    The receipt body itself is free-form by design."""
    with pytest.raises(ValueError, match="64-hex"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="too-short",
            adapter="x402", settlement_choice="x402:usdc", outcome="success",
        )
    with pytest.raises(ValueError, match="not valid hex"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="z" * 64,   # right length, not hex
            adapter="x402", settlement_choice="x402:usdc", outcome="success",
        )
    with pytest.raises(ValueError, match="outcome"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="x402", settlement_choice="x402:usdc", outcome="maybe",
        )
    with pytest.raises(ValueError, match="adapter"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="", settlement_choice="x402:usdc", outcome="success",
        )
    with pytest.raises(ValueError, match="settlement_choice"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="x402", settlement_choice="no_colon", outcome="success",
        )


# ===== T5-#4: replay across the full lifecycle =====


def test_T5_replay_returns_full_mandate_audit_chain(bus, dao, seller, agent_did):
    """The whole point of reserving the four event types: a consumer
    can fetch the entire Mandate audit trail with one replay call."""
    intent = _make_intent(dao, agent_did)
    cart = _make_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, seller, cart_mandate_digest(cart))

    e1 = emit_intent_issued(bus, intent)
    e2 = emit_cart_received(bus, cart)
    e3 = emit_payment_authorised(bus, payment)
    e4 = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=payment_mandate_digest(payment),
        adapter="x402",
        settlement_choice="x402:usdc",
        outcome="success",
        receipt={"tx_hash": "0x1234"},
    )

    # Some unrelated event must NOT show up in the lifecycle filter
    bus.emit("unrelated.thing", {"noise": True})

    types = list(MANDATE_LIFECYCLE_EVENT_TYPES)
    chain = list(bus.replay(event_types=types))
    assert [e.event_id for e in chain] == [
        e1.event_id, e2.event_id, e3.event_id, e4.event_id,
    ]

    # And the digest binding chain is intact across events
    assert chain[1].payload["intent_mandate_digest"] == chain[0].payload["intent_mandate_digest"]
    assert chain[2].payload["cart_mandate_digest"] == chain[1].payload["cart_mandate_digest"]
    assert chain[3].payload["payment_mandate_digest"] == chain[2].payload["payment_mandate_digest"]


def test_T5_unrecognised_event_type_can_still_emit_unrestricted(bus):
    """The reserved set is a VOCABULARY, not a whitelist. EventBus
    keeps its open-vocabulary contract; only the four mandate strings
    are documented as having canonical payload shapes."""
    ev = bus.emit("some.other.thing", {"x": 1})
    assert ev.event_type == "some.other.thing"   # accepted, just not in the reserved set
    assert ev.event_type not in MANDATE_LIFECYCLE_EVENT_TYPES
