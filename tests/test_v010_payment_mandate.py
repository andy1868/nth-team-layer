"""Tests for nth_dao.mandate.payment (v0.10 Sprint Zero T-3).

PaymentMandate is the DAO's acceptance of a CartMandate, binding to
its digest and committing to a specific settlement rail. It closes
the AP2-shape Mandate triad: Intent (authorise) -> Cart (offer) ->
Payment (accept).

10 tests + 2 bonus covering W3C VC shape, sign+verify round trip,
proofPurpose enforcement, the three payment_satisfies_cart checks
(digest binding, settlement_choice in offered, payee = cart issuer),
issuer continuity, end-to-end triad chain, facade re-export.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    sign_cart_mandate,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    PAYMENT_CONTEXT,
    PAYMENT_TYPE,
    PROOF_PURPOSE as PAYMENT_PROOF_PURPOSE,
    build_payment_mandate,
    complete_triad_chain,
    payment_mandate_digest,
    payment_satisfies_cart,
    sign_payment_mandate,
    verify_payment_mandate,
)


# ===== fixtures =====


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="dao-treasury")


@pytest.fixture
def counterparty() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="reviewer-shop")


@pytest.fixture
def hijacker() -> AgentIdentity:
    """A different DAO that tries to accept a cart it didn't authorise."""
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="rogue-dao")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob").as_did()


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _make_signed_intent(dao: AgentIdentity, agent_did: str, **overrides):
    constraints = {
        "max_amount": {"value": "100.00", "currency": "USDC"},
        "allowed_counterparties": [],
        "allowed_settlement_methods": ["x402:usdc", "ap2:card"],
    }
    constraints.update(overrides.pop("constraints_overrides", {}))
    intent = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints=constraints,
        expires_at=_future(86400),
        **overrides,
    )
    return sign_intent_mandate(intent, dao)


def _make_signed_cart(
    counterparty: AgentIdentity, agent_did: str, intent_digest: str, **overrides
):
    cart = build_cart_mandate(
        issuer_did=counterparty.as_did(),
        buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "Code review of PR #42", "quantity": 1}],
        total=overrides.pop("total", {"value": "50.00", "currency": "USDC"}),
        settlement_methods=overrides.pop("settlement_methods", ["x402:usdc"]),
        expires_at=_future(3600),
        **overrides,
    )
    return sign_cart_mandate(cart, counterparty)


def _make_payment(dao: AgentIdentity, counterparty: AgentIdentity, cart_digest: str, **overrides):
    return build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice=overrides.pop("settlement_choice", "x402:usdc"),
        expires_at=_future(900),
        **overrides,
    )


# ===== 1. W3C VC shape =====


def test_T3_01_build_has_w3c_vc_shape(dao, counterparty, agent_did):
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    assert payment["@context"] == PAYMENT_CONTEXT
    assert payment["type"] == PAYMENT_TYPE
    assert payment["issuer"] == dao.as_did()
    assert "issuanceDate" in payment
    assert "validFrom" in payment
    assert "validUntil" in payment
    assert "credentialSubject" in payment
    assert "proof" not in payment


# ===== 2. credentialSubject content =====


def test_T3_02_credentialSubject_binds_cart_and_carries_choice(
    dao, counterparty, agent_did,
):
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    cart_digest = cart_mandate_digest(cart)
    payment = _make_payment(
        dao, counterparty, cart_digest,
        settlement_metadata={"recipient_address": "0xabc123"},
    )
    subject = payment["credentialSubject"]
    assert subject["id"] == counterparty.as_did()
    assert subject["cart_mandate_digest"] == cart_digest
    assert subject["settlement_choice"] == "x402:usdc"
    assert subject["settlement_metadata"] == {"recipient_address": "0xabc123"}
    assert len(subject["payment_id"]) == 16


# ===== 3. sign attaches capabilityInvocation proof =====


def test_T3_03_sign_attaches_proof_with_capabilityInvocation(
    dao, counterparty, agent_did,
):
    """PaymentMandate is a DELEGATION (DAO authorises settlement on this
    cart). proofPurpose must be capabilityInvocation - same as
    IntentMandate, distinct from CartMandate's assertionMethod."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    signed = sign_payment_mandate(payment, dao)
    assert "proof" not in payment   # input not mutated
    proof = signed["proof"]
    assert proof["type"] == "Ed25519Signature2020"
    assert proof["proofPurpose"] == "capabilityInvocation"
    assert proof["proofPurpose"] == PAYMENT_PROOF_PURPOSE
    assert proof["verificationMethod"].startswith(dao.as_did() + "#")
    assert len(proof["proofValue"]) == 128


# ===== 4. signed payment verifies =====


def test_T3_04_verify_signed_payment_passes(dao, counterparty, agent_did):
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    signed = sign_payment_mandate(payment, dao)
    ok, reason = verify_payment_mandate(signed)
    assert ok, reason


# ===== 5. tampered settlement_choice invalidates signature =====


def test_T3_05_verify_rejects_tampered_settlement_choice(dao, counterparty, agent_did):
    """An attacker can't swap settlement rails on a signed payment
    without invalidating the signature - the choice is part of the
    signed payload."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    signed = sign_payment_mandate(payment, dao)
    signed["credentialSubject"]["settlement_choice"] = "manual:ach"   # tampered
    ok, reason = verify_payment_mandate(signed)
    assert not ok
    assert "signature invalid" in reason


# ===== 6. wrong proofPurpose rejected =====


def test_T3_06_verify_rejects_wrong_proof_purpose(dao, counterparty, agent_did):
    """A payment signed with assertionMethod (the WRONG purpose for an
    authorisation) must be rejected at the gate so callers never treat
    a sale-shaped credential as a settlement authority."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    signed = sign_payment_mandate(payment, dao)
    signed["proof"]["proofPurpose"] = "assertionMethod"
    ok, reason = verify_payment_mandate(signed)
    assert not ok
    assert "wrong proof purpose" in reason


# ===== 7. payment_satisfies_cart happy path =====


def test_T3_07_satisfies_cart_for_compatible_payment(dao, counterparty, agent_did):
    """payment.cart_mandate_digest == digest(cart), settlement_choice
    is in cart.settlement_methods, payee = cart.issuer -> ok."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    ok, reason = payment_satisfies_cart(payment, cart)
    assert ok, reason


# ===== 8. digest mismatch rejected (swap defence) =====


def test_T3_08_satisfies_rejects_cart_digest_mismatch(dao, counterparty, agent_did):
    """A payment bound to cart A presented during settlement with cart
    B must fail. Without this check an attacker could swap a cheap
    cart for an expensive one at the last moment."""
    intent = _make_signed_intent(dao, agent_did)
    cart_a = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    cart_b = _make_signed_cart(
        counterparty, agent_did, intent_mandate_digest(intent),
        total={"value": "99.00", "currency": "USDC"},
    )
    payment_for_a = _make_payment(dao, counterparty, cart_mandate_digest(cart_a))
    ok, reason = payment_satisfies_cart(payment_for_a, cart_b)
    assert not ok
    assert "cart digest mismatch" in reason


# ===== 9. unilateral rail choice rejected =====


def test_T3_09_satisfies_rejects_unilateral_settlement_choice(
    dao, counterparty, agent_did,
):
    """The DAO can't unilaterally pick a settlement rail the
    counterparty didn't offer. The cart's settlement_methods is the
    set of acceptable choices."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(
        counterparty, agent_did, intent_mandate_digest(intent),
        settlement_methods=["x402:usdc"],   # only x402 offered
    )
    payment_with_unoffered = _make_payment(
        dao, counterparty, cart_mandate_digest(cart),
        settlement_choice="ap2:card",   # not in offered
    )
    ok, reason = payment_satisfies_cart(payment_with_unoffered, cart)
    assert not ok
    assert "settlement_choice" in reason


# ===== 10. payee redirection rejected =====


def test_T3_10_satisfies_rejects_payee_redirection(
    dao, counterparty, hijacker, agent_did,
):
    """The payment's payee (credentialSubject.id) MUST equal the cart
    issuer - otherwise the DAO would be redirecting funds to a third
    party that didn't make the offer."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    # Build a payment that names a DIFFERENT DID as the payee
    payment = build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=hijacker.as_did(),   # not the cart issuer
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc",
        expires_at=_future(900),
    )
    ok, reason = payment_satisfies_cart(payment, cart)
    assert not ok
    assert "payee mismatch" in reason


# ===== Bonus A: end-to-end triad chain =====


def test_T3_bonus_complete_triad_chain_happy_path(dao, counterparty, agent_did):
    """The full Intent -> Cart -> Payment chain, every check
    composes through complete_triad_chain."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = sign_payment_mandate(
        _make_payment(dao, counterparty, cart_mandate_digest(cart)),
        dao,
    )
    ok, reason = complete_triad_chain(intent, cart, payment)
    assert ok, reason


def test_T3_bonus_complete_triad_rejects_hijacked_issuer(
    dao, counterparty, hijacker, agent_did,
):
    """A DIFFERENT DAO can't sign the PaymentMandate against a cart
    bound to the original DAO's intent. complete_triad_chain catches
    issuer mismatch as the final gate."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    # Hijacker tries to accept the cart - same payee, same digest, but
    # the issuer is wrong
    payment = build_payment_mandate(
        issuer_did=hijacker.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc",
        expires_at=_future(900),
    )
    signed = sign_payment_mandate(payment, hijacker)
    ok, reason = complete_triad_chain(intent, cart, signed)
    assert not ok
    assert "issuer mismatch" in reason


# ===== Bonus B: facade re-export + digest stable =====


def test_T3_bonus_facade_and_digest_stable(dao, counterparty, agent_did):
    import nth_dao
    assert nth_dao.build_payment_mandate is build_payment_mandate
    assert nth_dao.sign_payment_mandate is sign_payment_mandate
    assert nth_dao.verify_payment_mandate is verify_payment_mandate
    assert nth_dao.payment_satisfies_cart is payment_satisfies_cart
    assert nth_dao.complete_triad_chain is complete_triad_chain

    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    d1 = payment_mandate_digest(payment)
    signed = sign_payment_mandate(payment, dao)
    d2 = payment_mandate_digest(signed)
    assert d1 == d2   # signing doesn't change the digest
