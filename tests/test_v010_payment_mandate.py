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


def _make_signed_intent(
    dao: AgentIdentity, agent_did: str,
    *,
    counterparty: AgentIdentity = None,
    **overrides,
):
    """Build + sign an IntentMandate.

    Post Voss V-22, the empty ``allowed_counterparties=[]`` default
    means fail-closed. The test fixture instead whitelists the
    supplied counterparty (if any) so the happy-path tests work
    without weakening the new semantic.
    """
    constraints = {
        "max_amount": {"value": "100.00", "currency": "USDC"},
        "allowed_counterparties": (
            [counterparty.as_did()] if counterparty is not None else []
        ),
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


def _make_payment(
    dao: AgentIdentity, counterparty: AgentIdentity, cart_digest: str,
    *, sign: bool = True, **overrides,
):
    """Build (and by default sign) a PaymentMandate.

    Voss V-21 requires payment_satisfies_cart inputs to carry a proof
    block; the default ``sign=True`` makes the happy-path callers
    work without per-test boilerplate.
    """
    mandate = build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice=overrides.pop("settlement_choice", "x402:usdc"),
        expires_at=_future(900),
        **overrides,
    )
    if sign:
        return sign_payment_mandate(mandate, dao)
    return mandate


# ===== 1. W3C VC shape =====


def test_T3_01_build_has_w3c_vc_shape(dao, counterparty, agent_did):
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart), sign=False)
    # V-11: PAYMENT_CONTEXT / PAYMENT_TYPE are immutable tuples;
    # on-wire shape is still a list.
    assert payment["@context"] == list(PAYMENT_CONTEXT)
    assert payment["type"] == list(PAYMENT_TYPE)
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
        dao, counterparty, cart_digest, sign=False,
        settlement_metadata={"recipient_address": "0xabc123"},
    )
    subject = payment["credentialSubject"]
    assert subject["id"] == counterparty.as_did()
    assert subject["cart_mandate_digest"] == cart_digest
    assert subject["settlement_choice"] == "x402:usdc"
    assert subject["settlement_metadata"] == {"recipient_address": "0xabc123"}
    # V-10: full UUID4 hex
    assert len(subject["payment_id"]) == 32


# ===== 3. sign attaches capabilityInvocation proof =====


def test_T3_03_sign_attaches_proof_with_capabilityInvocation(
    dao, counterparty, agent_did,
):
    """PaymentMandate is a DELEGATION (DAO authorises settlement on this
    cart). proofPurpose must be capabilityInvocation - same as
    IntentMandate, distinct from CartMandate's assertionMethod."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart), sign=False)
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
    # V-18: _make_payment already signs by default.
    signed = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    ok, reason = verify_payment_mandate(signed)
    assert ok, reason


# ===== 5. tampered settlement_choice invalidates signature =====


def test_T3_05_verify_rejects_tampered_settlement_choice(dao, counterparty, agent_did):
    """An attacker can't swap settlement rails on a signed payment
    without invalidating the signature - the choice is part of the
    signed payload."""
    intent = _make_signed_intent(dao, agent_did)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    signed = _make_payment(dao, counterparty, cart_mandate_digest(cart))
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
    # V-18: _make_payment signs by default; tamper proofPurpose in place
    signed = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    signed["proof"]["proofPurpose"] = "assertionMethod"
    ok, reason = verify_payment_mandate(signed)
    assert not ok
    assert "wrong proof purpose" in reason


# ===== 7. payment_satisfies_cart happy path =====


def test_T3_07_satisfies_cart_for_compatible_payment(dao, counterparty, agent_did):
    """payment.cart_mandate_digest == digest(cart), settlement_choice
    is in cart.settlement_methods, payee = cart.issuer -> ok.

    Post Voss V-21 + V-22: intent whitelists the counterparty; helper
    signs the payment by default so V-21 (require_signed) is satisfied.
    """
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    ok, reason = payment_satisfies_cart(payment, cart)
    assert ok, reason


# ===== 8. digest mismatch rejected (swap defence) =====


def test_T3_08_satisfies_rejects_cart_digest_mismatch(dao, counterparty, agent_did):
    """A payment bound to cart A presented during settlement with cart
    B must fail. Without this check an attacker could swap a cheap
    cart for an expensive one at the last moment."""
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
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
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
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
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    # Build a payment that names a DIFFERENT DID as the payee. Sign
    # it so the V-21 require_signed gate passes and we reach the
    # payee-mismatch check we're trying to test.
    payment = sign_payment_mandate(build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=hijacker.as_did(),   # not the cart issuer
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc",
        expires_at=_future(900),
    ), dao)
    ok, reason = payment_satisfies_cart(payment, cart)
    assert not ok
    assert "payee mismatch" in reason


# ===== Bonus A: end-to-end triad chain =====


def test_T3_bonus_complete_triad_chain_happy_path(dao, counterparty, agent_did):
    """The full Intent -> Cart -> Payment chain, every check
    composes through complete_triad_chain."""
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    # _make_payment now signs by default
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    ok, reason = complete_triad_chain(intent, cart, payment)
    assert ok, reason


def test_T3_bonus_complete_triad_rejects_expired_mandates(
    dao, counterparty, agent_did,
):
    """The settlement gate must reject expired authority even when all
    signatures and digest bindings are otherwise valid."""
    issued = datetime(2025, 1, 1, tzinfo=timezone.utc)
    expired_at = "2025-01-02T00:00:00+00:00"
    intent = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [counterparty.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=expired_at,
        issued_at=issued,
    )
    intent = sign_intent_mandate(intent, dao, created_at=issued)
    cart = build_cart_mandate(
        issuer_did=counterparty.as_did(),
        buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(intent),
        items=[{"description": "Code review of PR #42", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"],
        expires_at=expired_at,
        issued_at=issued,
    )
    cart = sign_cart_mandate(cart, counterparty, created_at=issued)
    payment = build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc",
        expires_at=expired_at,
        issued_at=issued,
    )
    payment = sign_payment_mandate(payment, dao, created_at=issued)

    ok, reason = complete_triad_chain(intent, cart, payment)
    assert not ok
    assert "expired" in reason


def test_T3_bonus_complete_triad_rejects_malformed_expiry(
    dao, counterparty, agent_did,
):
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
    cart = _make_signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _make_payment(dao, counterparty, cart_mandate_digest(cart))
    payment = {**payment, "validUntil": "not-an-iso-date"}

    ok, reason = complete_triad_chain(intent, cart, payment)
    assert not ok
    assert "malformed validUntil" in reason


def test_T3_bonus_complete_triad_rejects_hijacked_issuer(
    dao, counterparty, hijacker, agent_did,
):
    """A DIFFERENT DAO can't sign the PaymentMandate against a cart
    bound to the original DAO's intent. complete_triad_chain catches
    issuer mismatch as the very first gate (Voss V-31 reordering).
    Failure message uses the canonical "issuer continuity broken"
    phrasing emitted by both complete_triad_chain and
    payment_satisfies_cart."""
    intent = _make_signed_intent(dao, agent_did, counterparty=counterparty)
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
    assert "issuer continuity broken" in reason


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
    # V-18: build unsigned then sign once; the digest must match
    # before and after signing.
    unsigned_payment = _make_payment(
        dao, counterparty, cart_mandate_digest(cart), sign=False,
    )
    d1 = payment_mandate_digest(unsigned_payment)
    signed = sign_payment_mandate(unsigned_payment, dao)
    d2 = payment_mandate_digest(signed)
    assert d1 == d2   # signing doesn't change the digest
