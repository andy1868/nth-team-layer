"""T-1.1 horizontal: V-2..V-20 mirror in cart.py and payment.py.

The first Voss round hardened intent.py; the cart and payment files
were structurally identical copies of the pre-fix code. This file
pins the mirror: every V-x finding (except V-1/V-9 which T-11 covers,
V-15 which is intent-specific, and the constraints-shape items which
don't apply) must hold for cart and payment too.

Coverage matrix:

  V-4   cart._validate_money rejects NaN / Inf / sci / whitespace
        (intent's _validate_max_amount already does so)
  V-6   verify_cart_mandate / verify_payment_mandate return
        VerifyResult NamedTuple (bool semantics fixed)
  V-8   cart_expiry_status / payment_expiry_status distinguish
        VALID / EXPIRED / MALFORMED
  V-10  cart_id / payment_id keep full 32-hex UUID4
  V-11  CART_CONTEXT / CART_TYPE / PAYMENT_CONTEXT / PAYMENT_TYPE
        are immutable Final tuples
  V-12  validUntil > issuanceDate enforced at build time, with
        per-mandate _MAX_VALIDITY cap
  V-13  digest stable across signing (validates _strip_proof reuse
        across sign / verify / digest)
  V-18  re-signing rejected for both cart and payment
  V-20  verify failures emit a structured log line
  V-19  CART_PROOF_PURPOSE / PAYMENT_PROOF_PURPOSE canonical names
        re-exported via facade

V-7 (raise on cannot-determine) and V-14 (lazy import elimination)
are inherited transparently via the shared _data_integrity helper -
no separate mirror test needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate import cart as cart_module
from nth_dao.mandate import payment as payment_module
from nth_dao.mandate.cart import (
    CART_CONTEXT,
    CART_PROOF_PURPOSE,
    CART_TYPE,
    build_cart_mandate,
    cart_expiry_status,
    cart_mandate_digest,
    is_cart_expired,
    sign_cart_mandate,
    verify_cart_mandate,
)
from nth_dao.mandate.intent import (
    ExpiryStatus,
    VerifyResult,
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    PAYMENT_CONTEXT,
    PAYMENT_PROOF_PURPOSE,
    PAYMENT_TYPE,
    build_payment_mandate,
    is_payment_expired,
    payment_expiry_status,
    payment_mandate_digest,
    sign_payment_mandate,
    verify_payment_mandate,
)


# ----- fixtures -----


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t12-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t12-seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t12-agent").as_did()


def _future(s: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()


def _past(s: int = 3600) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=s)).isoformat()


def _signed_intent(dao, agent_did, *, seller=None) -> Dict[str, Any]:
    m = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did, purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": (
                [seller.as_did()] if seller is not None else []
            ),
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    return sign_intent_mandate(m, dao)


def _unsigned_cart(seller, agent_did, intent_digest) -> Dict[str, Any]:
    return build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    )


def _signed_cart(seller, agent_did, intent_digest) -> Dict[str, Any]:
    return sign_cart_mandate(_unsigned_cart(seller, agent_did, intent_digest), seller)


def _unsigned_payment(dao, seller, cart_digest) -> Dict[str, Any]:
    return build_payment_mandate(
        issuer_did=dao.as_did(), payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice="x402:usdc", expires_at=_future(900),
    )


def _signed_payment(dao, seller, cart_digest) -> Dict[str, Any]:
    return sign_payment_mandate(_unsigned_payment(dao, seller, cart_digest), dao)


# =====================================================================
# V-4: cart._validate_money rejects NaN / Inf / sci / whitespace
# =====================================================================


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "inf"])
def test_T12_V4_cart_total_rejects_non_finite(seller, agent_did, bad):
    with pytest.raises(ValueError):
        build_cart_mandate(
            issuer_did=seller.as_did(), buyer_did=agent_did,
            intent_mandate_digest_hex="a" * 64,
            items=[{"description": "x", "quantity": 1}],
            total={"value": bad, "currency": "USDC"},
            settlement_methods=["x402:usdc"], expires_at=_future(3600),
        )


@pytest.mark.parametrize("bad", ["1e10", "1.5E5"])
def test_T12_V4_cart_total_rejects_scientific(seller, agent_did, bad):
    with pytest.raises(ValueError, match="scientific"):
        build_cart_mandate(
            issuer_did=seller.as_did(), buyer_did=agent_did,
            intent_mandate_digest_hex="a" * 64,
            items=[{"description": "x", "quantity": 1}],
            total={"value": bad, "currency": "USDC"},
            settlement_methods=["x402:usdc"], expires_at=_future(3600),
        )


@pytest.mark.parametrize("bad", [" 100", "100 ", "\t100"])
def test_T12_V4_cart_total_rejects_whitespace(seller, agent_did, bad):
    with pytest.raises(ValueError, match="whitespace"):
        build_cart_mandate(
            issuer_did=seller.as_did(), buyer_did=agent_did,
            intent_mandate_digest_hex="a" * 64,
            items=[{"description": "x", "quantity": 1}],
            total={"value": bad, "currency": "USDC"},
            settlement_methods=["x402:usdc"], expires_at=_future(3600),
        )


# =====================================================================
# V-6: VerifyResult NamedTuple semantics for cart and payment
# =====================================================================


def test_T12_V6_verify_cart_returns_VerifyResult_with_bool(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    good = verify_cart_mandate(cart)
    assert isinstance(good, VerifyResult)
    assert bool(good) is True
    # The classic-but-broken `if not verify(...)` idiom is now safe
    if good:
        pass
    else:
        pytest.fail("good cart should be truthy")

    tampered = dict(cart)
    tampered["credentialSubject"] = dict(tampered["credentialSubject"])
    tampered["credentialSubject"]["total"] = {
        "value": "0.01", "currency": "USDC",
    }
    bad = verify_cart_mandate(tampered)
    assert bool(bad) is False
    assert not bad
    # Tuple unpacking still works
    ok, reason = bad
    assert ok is False and reason


def test_T12_V6_verify_payment_returns_VerifyResult(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    good = verify_payment_mandate(payment)
    assert isinstance(good, VerifyResult)
    assert bool(good) is True


def test_T12_V6_VerifyResult_shared_across_three_mandates():
    """cart and payment must return the SAME VerifyResult class as
    intent (via re-export), so callers can do generic
    ``isinstance(result, VerifyResult)`` checks across mandate kinds."""
    # All three import VerifyResult from intent.py; assert it's
    # actually the same identity, not a copy.
    from nth_dao.mandate.cart import VerifyResult as CartVR
    from nth_dao.mandate.payment import VerifyResult as PaymentVR
    assert CartVR is VerifyResult
    assert PaymentVR is VerifyResult


# =====================================================================
# V-8: cart_expiry_status / payment_expiry_status tristate
# =====================================================================


def test_T12_V8_cart_expiry_status_valid(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    assert cart_expiry_status(cart) == ExpiryStatus.VALID
    assert is_cart_expired(cart) is False


def test_T12_V8_cart_expiry_status_expired(dao, seller, agent_did):
    """Construct a legit issuanceDate in the past + validUntil also in
    the past (V-12 allows both as long as validUntil > issuanceDate)."""
    issued = datetime.now(timezone.utc) - timedelta(hours=2)
    expires = (issued + timedelta(hours=1)).isoformat()
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(intent),
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"],
        expires_at=expires, issued_at=issued,
    )
    assert cart_expiry_status(cart) == ExpiryStatus.EXPIRED
    assert is_cart_expired(cart) is True


@pytest.mark.parametrize("bad_until", [None, "", "garbage", 42, "2026-13-99T00:00:00+00:00"])
def test_T12_V8_cart_expiry_status_malformed(bad_until):
    """Malformed timestamps are NOT EXPIRED - they're MALFORMED."""
    mandate = {"validUntil": bad_until}
    assert cart_expiry_status(mandate) == ExpiryStatus.MALFORMED
    assert is_cart_expired(mandate) is False


def test_T12_V8_payment_expiry_status_valid(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    assert payment_expiry_status(payment) == ExpiryStatus.VALID
    assert is_payment_expired(payment) is False


def test_T12_V8_payment_expiry_status_malformed():
    assert payment_expiry_status({"validUntil": "garbage"}) == ExpiryStatus.MALFORMED


# =====================================================================
# V-10: full UUID4 hex (32 chars)
# =====================================================================


def test_T12_V10_cart_id_full_uuid(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    assert len(cart["credentialSubject"]["cart_id"]) == 32


def test_T12_V10_payment_id_full_uuid(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    assert len(payment["credentialSubject"]["payment_id"]) == 32


def test_T12_V10_explicit_id_round_trips(seller, agent_did):
    explicit_cart = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex="a" * 64,
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
        cart_id="explicit-cart-id",
    )
    assert explicit_cart["credentialSubject"]["cart_id"] == "explicit-cart-id"


# =====================================================================
# V-11: immutable Final tuple constants
# =====================================================================


def test_T12_V11_cart_constants_immutable():
    assert isinstance(CART_CONTEXT, tuple)
    assert isinstance(CART_TYPE, tuple)
    with pytest.raises((AttributeError, TypeError)):
        CART_CONTEXT.append("https://evil")   # type: ignore[attr-defined]


def test_T12_V11_payment_constants_immutable():
    assert isinstance(PAYMENT_CONTEXT, tuple)
    assert isinstance(PAYMENT_TYPE, tuple)
    with pytest.raises((AttributeError, TypeError)):
        PAYMENT_CONTEXT.append("https://evil")   # type: ignore[attr-defined]


def test_T12_V11_built_mandate_has_fresh_lists(dao, seller, agent_did):
    """Mutating a built mandate's @context doesn't leak into future
    mandates."""
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart_one = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    cart_one["@context"].append("https://evil")
    cart_two = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    assert "https://evil" not in cart_two["@context"]


# =====================================================================
# V-12: validUntil > issuanceDate + max validity cap
# =====================================================================


def test_T12_V12_cart_validUntil_must_be_after_issuance(seller, agent_did):
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="strictly after"):
        build_cart_mandate(
            issuer_did=seller.as_did(), buyer_did=agent_did,
            intent_mandate_digest_hex="a" * 64,
            items=[{"description": "x", "quantity": 1}],
            total={"value": "50.00", "currency": "USDC"},
            settlement_methods=["x402:usdc"],
            expires_at=(issued - timedelta(seconds=1)).isoformat(),
            issued_at=issued,
        )


def test_T12_V12_cart_validity_cap(seller, agent_did):
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="exceeds"):
        build_cart_mandate(
            issuer_did=seller.as_did(), buyer_did=agent_did,
            intent_mandate_digest_hex="a" * 64,
            items=[{"description": "x", "quantity": 1}],
            total={"value": "50.00", "currency": "USDC"},
            settlement_methods=["x402:usdc"],
            expires_at=(issued + timedelta(days=365 * 100)).isoformat(),
            issued_at=issued,
        )


def test_T12_V12_payment_validUntil_must_be_after_issuance(dao, seller):
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="strictly after"):
        build_payment_mandate(
            issuer_did=dao.as_did(), payee_did=seller.as_did(),
            cart_mandate_digest_hex="a" * 64,
            settlement_choice="x402:usdc",
            expires_at=(issued - timedelta(seconds=1)).isoformat(),
            issued_at=issued,
        )


def test_T12_V12_payment_validity_cap(dao, seller):
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="exceeds"):
        build_payment_mandate(
            issuer_did=dao.as_did(), payee_did=seller.as_did(),
            cart_mandate_digest_hex="a" * 64,
            settlement_choice="x402:usdc",
            expires_at=(issued + timedelta(days=365 * 100)).isoformat(),
            issued_at=issued,
        )


# =====================================================================
# V-13: digest stable across signing (validates centralised _strip_proof)
# =====================================================================


def test_T12_V13_cart_digest_stable_across_signing(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    unsigned_cart = _unsigned_cart(seller, agent_did, intent_mandate_digest(intent))
    pre = cart_mandate_digest(unsigned_cart)
    signed = sign_cart_mandate(unsigned_cart, seller)
    post = cart_mandate_digest(signed)
    assert pre == post


def test_T12_V13_payment_digest_stable_across_signing(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    unsigned = _unsigned_payment(dao, seller, cart_mandate_digest(cart))
    pre = payment_mandate_digest(unsigned)
    signed = sign_payment_mandate(unsigned, dao)
    post = payment_mandate_digest(signed)
    assert pre == post


# =====================================================================
# V-18: re-sign rejection for cart and payment
# =====================================================================


def test_T12_V18_cart_resigning_rejected(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    signed = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    with pytest.raises(ValueError, match="already carries a proof"):
        sign_cart_mandate(signed, seller)


def test_T12_V18_payment_resigning_rejected(dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    signed = _signed_payment(dao, seller, cart_mandate_digest(cart))
    with pytest.raises(ValueError, match="already carries a proof"):
        sign_payment_mandate(signed, dao)


# =====================================================================
# V-20: verify failures emit a structured log line
# =====================================================================


def test_T12_V20_cart_verify_failure_logged(dao, seller, agent_did, caplog):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    # Tamper
    cart["credentialSubject"] = dict(cart["credentialSubject"])
    cart["credentialSubject"]["total"] = {"value": "0.01", "currency": "USDC"}
    with caplog.at_level(logging.INFO, logger="nth_dao.mandate.cart"):
        result = verify_cart_mandate(cart)
    assert result.ok is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "cart_mandate verify failed" in m and seller.as_did() in m
        for m in msgs
    ), f"expected forensic log line, got: {msgs}"


def test_T12_V20_payment_verify_failure_logged(dao, seller, agent_did, caplog):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    payment["credentialSubject"] = dict(payment["credentialSubject"])
    payment["credentialSubject"]["settlement_choice"] = "x402:wrong"
    with caplog.at_level(logging.INFO, logger="nth_dao.mandate.payment"):
        result = verify_payment_mandate(payment)
    assert result.ok is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "payment_mandate verify failed" in m and dao.as_did() in m
        for m in msgs
    )


# =====================================================================
# V-19: canonical PROOF_PURPOSE names re-exported via facade
# =====================================================================


def test_T12_V19_canonical_proof_purpose_names():
    assert CART_PROOF_PURPOSE == "assertionMethod"
    assert PAYMENT_PROOF_PURPOSE == "capabilityInvocation"
    # Legacy aliases still exposed for backwards compat
    assert cart_module.PROOF_PURPOSE == CART_PROOF_PURPOSE
    assert payment_module.PROOF_PURPOSE == PAYMENT_PROOF_PURPOSE


def test_T12_V19_mandate_subpackage_exposes_canonical_names():
    """The mandate facade re-exports canonical CART_PROOF_PURPOSE /
    PAYMENT_PROOF_PURPOSE names (in addition to the legacy
    PROOF_PURPOSE alias in each module). Top-level nth_dao re-export
    is intentionally not promised here - those names need a separate
    facade-update ticket since they collide cross-mandate."""
    from nth_dao import mandate
    assert mandate.CART_PROOF_PURPOSE == CART_PROOF_PURPOSE
    assert mandate.PAYMENT_PROOF_PURPOSE == PAYMENT_PROOF_PURPOSE
