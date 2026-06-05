"""Tests for nth_dao.mandate.cart (v0.10 Sprint Zero T-2).

CartMandate is the counterparty's OFFER bound to a specific
IntentMandate digest. Without cart_satisfies_intent, the intent's
constraints would be theatre.

10 tests covering W3C VC shape, sign+verify round trip, tamper/
proofPurpose/issuer mismatch rejections, AND the five-check
intent-satisfaction logic (digest binding, currency match, amount
limit, counterparty allow-list, settlement method overlap).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    CART_CONTEXT,
    CART_TYPE,
    PROOF_PURPOSE as CART_PROOF_PURPOSE,
    build_cart_mandate,
    cart_mandate_digest,
    cart_satisfies_intent,
    sign_cart_mandate,
    verify_cart_mandate,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
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
def other_seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="rogue-seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob").as_did()


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _make_intent(
    dao: AgentIdentity, agent_did: str,
    *,
    max_amount=None,
    allowed_counterparties=None,
    allowed_settlement_methods=None,
):
    """Build a representative IntentMandate; defaults to a generous
    100 USDC budget over x402:usdc with no counterparty restriction."""
    constraints = {}
    if max_amount is None:
        max_amount = {"value": "100.00", "currency": "USDC"}
    constraints["max_amount"] = max_amount
    constraints["allowed_counterparties"] = list(allowed_counterparties or [])
    constraints["allowed_settlement_methods"] = list(
        allowed_settlement_methods or ["x402:usdc"]
    )
    return build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints=constraints,
        expires_at=_future(86400),
    )


def _make_cart(
    counterparty: AgentIdentity,
    buyer_did: str,
    digest_hex: str,
    *,
    total=None,
    settlement_methods=None,
):
    if total is None:
        total = {"value": "50.00", "currency": "USDC"}
    return build_cart_mandate(
        issuer_did=counterparty.as_did(),
        buyer_did=buyer_did,
        intent_mandate_digest_hex=digest_hex,
        items=[{"description": "Code review of PR #42", "quantity": 1}],
        total=total,
        settlement_methods=settlement_methods or ["x402:usdc"],
        expires_at=_future(3600),
    )


# ===== 1. W3C VC shape =====


def test_T2_01_build_has_w3c_vc_shape(dao, counterparty, agent_did):
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    assert cart["@context"] == CART_CONTEXT
    assert cart["type"] == CART_TYPE
    assert cart["type"][0] == "VerifiableCredential"
    assert cart["issuer"] == counterparty.as_did()
    assert "issuanceDate" in cart
    assert "validFrom" in cart
    assert "validUntil" in cart
    assert "credentialSubject" in cart
    assert "proof" not in cart


# ===== 2. credentialSubject content + binding =====


def test_T2_02_credentialSubject_binds_to_intent_digest(dao, counterparty, agent_did):
    """The cart MUST carry the intent's digest; that's the binding
    mechanism by which a payment processor knows the seller's offer
    matches the DAO's authorisation."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    subject = cart["credentialSubject"]
    assert subject["id"] == agent_did
    assert subject["intent_mandate_digest"] == digest
    assert len(subject["cart_id"]) == 16
    assert subject["total"] == {"value": "50.00", "currency": "USDC"}
    assert len(subject["items"]) == 1
    assert subject["settlement_methods"] == ["x402:usdc"]


# ===== 3. sign attaches proof with assertionMethod purpose =====


def test_T2_03_sign_attaches_proof_with_assertionMethod(dao, counterparty, agent_did):
    """CartMandate is an ASSERTION ("I will perform X for Y") - not
    a delegation. proofPurpose must be assertionMethod, distinct from
    IntentMandate's capabilityInvocation."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    signed = sign_cart_mandate(cart, counterparty)
    assert "proof" not in cart   # input not mutated
    proof = signed["proof"]
    assert proof["type"] == "Ed25519Signature2020"
    assert proof["proofPurpose"] == "assertionMethod"
    assert proof["proofPurpose"] == CART_PROOF_PURPOSE
    assert proof["verificationMethod"].startswith(counterparty.as_did() + "#")
    assert len(proof["proofValue"]) == 128


# ===== 4. signed cart verifies =====


def test_T2_04_verify_signed_cart_passes(dao, counterparty, agent_did):
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    signed = sign_cart_mandate(cart, counterparty)
    ok, reason = verify_cart_mandate(signed)
    assert ok, reason


# ===== 5. tampering total invalidates signature =====


def test_T2_05_verify_rejects_tampered_total(dao, counterparty, agent_did):
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    signed = sign_cart_mandate(cart, counterparty)
    # Buyer-side attacker: lower the price after signing
    signed["credentialSubject"]["total"]["value"] = "0.01"
    ok, reason = verify_cart_mandate(signed)
    assert not ok
    assert "signature invalid" in reason


# ===== 6. wrong proofPurpose rejected =====


def test_T2_06_verify_rejects_wrong_proof_purpose(dao, counterparty, agent_did):
    """A cart signed with capabilityInvocation (the WRONG purpose for
    an offer) must be rejected at the gate so callers never treat an
    authorisation-shaped credential as a sale."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    signed = sign_cart_mandate(cart, counterparty)
    signed["proof"]["proofPurpose"] = "capabilityInvocation"
    ok, reason = verify_cart_mandate(signed)
    assert not ok
    assert "wrong proof purpose" in reason


# ===== 7. cart_satisfies_intent happy path =====


def test_T2_07_satisfies_intent_for_compatible_cart(dao, counterparty, agent_did):
    """The DAO authorises 100 USDC via x402:usdc; the cart asks for
    50 USDC via x402:usdc - satisfies on all five axes."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    ok, reason = cart_satisfies_intent(cart, intent)
    assert ok, reason


# ===== 8. budget overrun rejected =====


def test_T2_08_satisfies_intent_rejects_total_over_max_amount(dao, counterparty, agent_did):
    """The whole point of IntentMandate.max_amount: the cart cannot
    exceed it. Decimal compare so "100.01" > "100.00"."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    # 100.01 vs max 100.00 - one cent over
    over_budget = _make_cart(
        counterparty, agent_did, digest,
        total={"value": "100.01", "currency": "USDC"},
    )
    ok, reason = cart_satisfies_intent(over_budget, intent)
    assert not ok
    assert "exceeds budget" in reason

    # Exactly at the cap is OK (boundary check)
    at_cap = _make_cart(
        counterparty, agent_did, digest,
        total={"value": "100.00", "currency": "USDC"},
    )
    ok2, reason2 = cart_satisfies_intent(at_cap, intent)
    assert ok2, reason2


# ===== 9. currency mismatch + digest mismatch rejected =====


def test_T2_09_satisfies_rejects_currency_and_digest_mismatch(dao, counterparty, agent_did):
    """A cart in EUR cannot satisfy a USDC intent (currency check).
    A cart with a stale or forged digest cannot satisfy any intent
    (binding check)."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)

    # Currency mismatch
    wrong_currency = _make_cart(
        counterparty, agent_did, digest,
        total={"value": "50.00", "currency": "EUR"},
    )
    ok, reason = cart_satisfies_intent(wrong_currency, intent)
    assert not ok
    assert "currency mismatch" in reason

    # Digest mismatch (cart bound to a DIFFERENT intent)
    other_intent = _make_intent(
        dao, agent_did,
        max_amount={"value": "999.00", "currency": "USDC"},
    )
    other_digest = intent_mandate_digest(other_intent)
    swap = _make_cart(counterparty, agent_did, other_digest)
    ok2, reason2 = cart_satisfies_intent(swap, intent)
    assert not ok2
    assert "intent digest mismatch" in reason2


# ===== 10. counterparty + settlement method allow-list =====


def test_T2_10_satisfies_rejects_disallowed_counterparty_and_method(
    dao, counterparty, other_seller, agent_did,
):
    """When the DAO restricts allowed_counterparties / settlement
    methods to a specific list, carts outside the list must fail."""
    intent = _make_intent(
        dao, agent_did,
        allowed_counterparties=[counterparty.as_did()],
        allowed_settlement_methods=["x402:usdc"],
    )
    digest = intent_mandate_digest(intent)

    # Wrong counterparty
    rogue_cart = _make_cart(other_seller, agent_did, digest)
    ok, reason = cart_satisfies_intent(rogue_cart, intent)
    assert not ok
    assert "allowed_counterparties" in reason

    # Right counterparty, wrong settlement method
    wrong_method = _make_cart(
        counterparty, agent_did, digest,
        settlement_methods=["ap2:card"],
    )
    ok2, reason2 = cart_satisfies_intent(wrong_method, intent)
    assert not ok2
    assert "allowed_settlement_methods" in reason2

    # Multiple methods offered, at least one matches -> OK
    multi_method = _make_cart(
        counterparty, agent_did, digest,
        settlement_methods=["ap2:card", "x402:usdc", "ach:wire"],
    )
    ok3, reason3 = cart_satisfies_intent(multi_method, intent)
    assert ok3, reason3


# ===== Bonus: facade re-export sanity =====


def test_T2_facade_reexport():
    import nth_dao
    assert nth_dao.build_cart_mandate is build_cart_mandate
    assert nth_dao.sign_cart_mandate is sign_cart_mandate
    assert nth_dao.verify_cart_mandate is verify_cart_mandate
    assert nth_dao.cart_mandate_digest is cart_mandate_digest
    assert nth_dao.cart_satisfies_intent is cart_satisfies_intent


# ===== Bonus: digest stable across signing + boundary helpers =====


def test_T2_digest_stable_and_helpers_work(dao, counterparty, agent_did):
    """T-3 PaymentMandate will bind to cart_mandate_digest, so the
    digest MUST be stable across signing - signing only adds the proof
    block, which is excluded."""
    intent = _make_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    cart = _make_cart(counterparty, agent_did, digest)
    d1 = cart_mandate_digest(cart)
    signed = sign_cart_mandate(cart, counterparty)
    d2 = cart_mandate_digest(signed)
    assert d1 == d2

    # Real signed intent + signed cart can be verified end-to-end
    signed_intent = sign_intent_mandate(intent, dao)
    intent_ok, _ = cart_satisfies_intent(signed, signed_intent)
    assert intent_ok
