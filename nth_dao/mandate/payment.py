"""PaymentMandate - the ACCEPTANCE half that closes the Mandate triad.

After:
  1. The DAO signed an IntentMandate authorising agent X to spend Y
  2. The counterparty signed a CartMandate offering goods at price Z
     bound to that intent's digest

...the DAO signs a PaymentMandate accepting the cart and committing to
a specific settlement rail. The triad is now complete:

  IntentMandate  -- authorises  -->  agent
  CartMandate    -- offers      -->  goods at price
  PaymentMandate -- accepts     -->  settles via chosen rail

The PaymentMandate is the signal a SettlementAdapter (x402, AP2 card,
manual, etc.) listens for. Its presence + its three binding checks
(payment->cart->intent) prove every external requirement was met:

  * The DAO authorised this purpose (intent)
  * The counterparty offered these terms (cart)
  * Both sides accept the specific settlement rail (payment)

Why proofPurpose=capabilityInvocation?
    Like IntentMandate, PaymentMandate is a DELEGATION - the DAO
    delegates settlement authority to the named SettlementAdapter for
    this specific cart. capabilityInvocation is the canonical VC Data
    Integrity proof purpose for delegations. CartMandate's
    assertionMethod (an offer being made) is semantically different;
    keeping them distinct prevents callers from confusing an offer
    with an authorisation.

Three-stage check chain (the end-to-end gate):

  1. verify_intent_mandate(intent)
     -> intent is authentic and signed by the DAO

  2. verify_cart_mandate(cart) AND cart_satisfies_intent(cart, intent)
     -> cart is authentic AND fits within the intent's constraints

  3. verify_payment_mandate(payment) AND
     payment_satisfies_cart(payment, cart) AND
     payment.issuer == intent.issuer
     -> payment is authentic AND picks an offered settlement rail
        AND is from the same DAO that issued the intent

complete_triad_chain(intent, cart, payment) does all of this in one
call for callers who don't want to compose manually.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..identity import AgentIdentity, canonical_json
from .cart import (
    cart_mandate_digest,
    cart_satisfies_intent,
    verify_cart_mandate,
)
from .intent import (
    _DID_KEY_RE,
    _check_iso_with_tz,
    verify_intent_mandate,
)

logger = logging.getLogger("nth_dao.mandate.payment")


PAYMENT_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://nth-dao.org/credentials/payment-mandate/v1",
]
PAYMENT_TYPE = ["VerifiableCredential", "PaymentMandate"]
PROOF_TYPE = "Ed25519Signature2020"
PROOF_PURPOSE = "capabilityInvocation"


# ===== build =====


def build_payment_mandate(
    issuer_did: str,
    payee_did: str,
    cart_mandate_digest_hex: str,
    settlement_choice: str,
    expires_at: str,
    *,
    settlement_metadata: Optional[Dict[str, Any]] = None,
    payment_id: Optional[str] = None,
    issued_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build an UNSIGNED PaymentMandate dict.

    Parameters
    ----------
    issuer_did
        did:key of the DAO accepting the cart. MUST equal the original
        IntentMandate's issuer - same authorising party throughout.
    payee_did
        did:key of the entity receiving payment - normally the
        CartMandate's issuer.
    cart_mandate_digest_hex
        SHA-256 hex of the CartMandate being accepted. ``cart_mandate_digest()``
        from .cart produces this.
    settlement_choice
        ``"<adapter>:<asset>"`` token; MUST be one of the methods the
        cart offered.
    expires_at
        ISO-8601 with timezone marker. How long the settlement adapter
        is authorised to attempt the actual rail transaction.
    settlement_metadata
        Adapter-specific routing info (e.g. ``{"recipient_address":
        "0x..."}`` for x402). Optional; the schema is defined by the
        chosen adapter, not this layer.
    payment_id
        Optional 16-hex unique id; auto-generated when omitted.
    issued_at
        Optional issuance time; defaults to ``datetime.now(UTC)``.
    """
    if not _DID_KEY_RE.match(issuer_did):
        raise ValueError(f"issuer_did must be a did:key, got {issuer_did!r}")
    if not _DID_KEY_RE.match(payee_did):
        raise ValueError(f"payee_did must be a did:key, got {payee_did!r}")
    if not isinstance(cart_mandate_digest_hex, str) or len(cart_mandate_digest_hex) != 64:
        raise ValueError(
            f"cart_mandate_digest must be a 64-hex SHA-256 string, "
            f"got {cart_mandate_digest_hex!r}"
        )
    try:
        bytes.fromhex(cart_mandate_digest_hex)
    except ValueError as exc:
        raise ValueError(
            f"cart_mandate_digest is not valid hex: {cart_mandate_digest_hex!r}"
        ) from exc
    if not isinstance(settlement_choice, str) or ":" not in settlement_choice:
        raise ValueError(
            f"settlement_choice must be '<adapter>:<asset>', got {settlement_choice!r}"
        )
    if settlement_metadata is not None and not isinstance(settlement_metadata, dict):
        raise ValueError("settlement_metadata must be a dict when provided")
    _check_iso_with_tz(expires_at, "expires_at")

    issued = (issued_at or datetime.now(timezone.utc)).isoformat()
    payment_id_value = payment_id or uuid.uuid4().hex[:16]

    subject: Dict[str, Any] = {
        "id": payee_did,
        "payment_id": payment_id_value,
        "cart_mandate_digest": cart_mandate_digest_hex,
        "settlement_choice": settlement_choice,
    }
    # Only include metadata key when actually present, so the canonical
    # JSON (and therefore the digest) is stable regardless of whether
    # the caller passed an empty dict vs no dict at all.
    if settlement_metadata:
        subject["settlement_metadata"] = dict(settlement_metadata)

    return {
        "@context": list(PAYMENT_CONTEXT),
        "type": list(PAYMENT_TYPE),
        "issuer": issuer_did,
        "issuanceDate": issued,
        "validFrom": issued,
        "validUntil": expires_at,
        "credentialSubject": subject,
    }


# ===== sign =====


def sign_payment_mandate(
    mandate: Dict[str, Any],
    identity: AgentIdentity,
    *,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attach Ed25519Signature2020 proof. Returns a NEW dict; input
    is not mutated.

    The signing identity's DID must match ``mandate.issuer``.
    """
    if not identity.can_sign:
        raise RuntimeError("identity has no signing key - cannot sign mandate")
    issuer = mandate.get("issuer", "")
    if not issuer:
        raise ValueError("mandate has no issuer; cannot sign")
    try:
        expected_did = identity.as_did()
    except Exception as exc:   # noqa: BLE001
        raise RuntimeError(f"identity cannot produce a DID: {exc}") from exc
    if issuer != expected_did:
        raise ValueError(
            f"issuer DID mismatch: mandate.issuer={issuer!r} but "
            f"identity DID is {expected_did!r}"
        )

    payload = {k: v for k, v in mandate.items() if k != "proof"}
    sig_hex = identity.sign_json(payload)
    created = (created_at or datetime.now(timezone.utc)).isoformat()
    proof = {
        "type": PROOF_TYPE,
        "created": created,
        "verificationMethod": f"{issuer}#{identity.pubkey_hex}",
        "proofPurpose": PROOF_PURPOSE,
        "proofValue": sig_hex,
    }
    return {**mandate, "proof": proof}


# ===== verify =====


def verify_payment_mandate(mandate: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify a signed PaymentMandate.

    Returns ``(ok, reason)``. Authenticity only - does NOT verify that
    the payment fits its cart or that the cart fits its intent. Pair
    with ``payment_satisfies_cart`` and ``cart_satisfies_intent``, or
    call ``complete_triad_chain`` for the full gate.
    """
    proof = mandate.get("proof")
    if not isinstance(proof, dict):
        return False, "missing proof"
    if proof.get("type") != PROOF_TYPE:
        return False, f"unsupported proof type: {proof.get('type')!r}"
    if proof.get("proofPurpose") != PROOF_PURPOSE:
        return False, (
            f"wrong proof purpose: {proof.get('proofPurpose')!r}; "
            f"PaymentMandate requires {PROOF_PURPOSE!r}"
        )
    sig_hex = proof.get("proofValue", "")
    if not sig_hex:
        return False, "missing proofValue"
    issuer = mandate.get("issuer", "")
    if not issuer.startswith("did:key:"):
        return False, f"unsupported issuer scheme: {issuer!r}"

    try:
        from ..did_key import decode_ed25519_did_key
        from nacl.signing import VerifyKey
    except ImportError as exc:
        return False, f"verification requires PyNaCl + did_key: {exc}"

    payload = {k: v for k, v in mandate.items() if k != "proof"}
    try:
        pubkey_bytes = decode_ed25519_did_key(issuer)
        VerifyKey(pubkey_bytes).verify(canonical_json(payload), bytes.fromhex(sig_hex))
    except Exception as exc:   # noqa: BLE001
        return False, f"signature invalid: {exc}"
    return True, "ok"


# ===== payment-cart binding check =====


def payment_satisfies_cart(
    payment: Dict[str, Any],
    cart: Dict[str, Any],
) -> Tuple[bool, str]:
    """Check that a PaymentMandate accepts a specific cart correctly.

    Returns ``(ok, reason)``. Three short-circuiting checks:

      1. payment.cart_mandate_digest == digest(cart)
         (swap-attack: payment can't claim acceptance of cart A while
          paired with cart B during settlement)
      2. payment.settlement_choice IS in cart.settlement_methods
         (the DAO can't unilaterally pick a rail the counterparty
          didn't offer)
      3. payment.credentialSubject.id == cart.issuer
         (the payee in the payment must be the same entity that issued
          the cart - prevents redirecting funds to a third party)

    Does NOT verify signatures - pair with verify_payment_mandate +
    verify_cart_mandate, or use complete_triad_chain.
    """
    if not isinstance(payment, dict) or not isinstance(cart, dict):
        return False, "payment and cart must be dicts"

    payment_subject = payment.get("credentialSubject", {})
    if not isinstance(payment_subject, dict):
        return False, "payment.credentialSubject malformed"

    # 1. digest binding
    declared_digest = payment_subject.get("cart_mandate_digest", "")
    actual_digest = cart_mandate_digest(cart)
    if declared_digest != actual_digest:
        return False, (
            f"cart digest mismatch: payment binds to "
            f"{declared_digest[:16]}..., cart digest is "
            f"{actual_digest[:16]}..."
        )

    # 2. settlement choice in cart's offer
    cart_subject = cart.get("credentialSubject", {})
    if not isinstance(cart_subject, dict):
        return False, "cart.credentialSubject malformed"
    chosen = payment_subject.get("settlement_choice", "")
    offered = cart_subject.get("settlement_methods", []) or []
    if chosen not in offered:
        return False, (
            f"settlement_choice {chosen!r} not in cart's offered "
            f"methods {offered}"
        )

    # 3. payee identity matches cart issuer
    payee = payment_subject.get("id", "")
    cart_issuer = cart.get("issuer", "")
    if payee != cart_issuer:
        return False, (
            f"payee mismatch: payment.subject.id={payee!r} but "
            f"cart.issuer={cart_issuer!r} - payment would redirect "
            "funds to a third party"
        )

    return True, "ok"


# ===== end-to-end triad =====


def complete_triad_chain(
    intent: Dict[str, Any],
    cart: Dict[str, Any],
    payment: Dict[str, Any],
) -> Tuple[bool, str]:
    """End-to-end gate: verify the entire Intent->Cart->Payment chain.

    Returns ``(ok, reason)``. A SettlementAdapter should call this
    once before initiating any external rail transaction. If it
    returns ok=True, the three mandates are mutually consistent and
    all three signatures are valid.

    Checks in order:

      A. intent is authentic                       (verify_intent_mandate)
      B. cart is authentic                         (verify_cart_mandate)
      C. cart satisfies intent (digest, amount,    (cart_satisfies_intent)
         currency, counterparty, method overlap)
      D. payment is authentic                      (verify_payment_mandate)
      E. payment satisfies cart (digest,           (payment_satisfies_cart)
         settlement_choice in offered, payee
         matches cart issuer)
      F. payment.issuer == intent.issuer
         (the same DAO that authorised the intent is the one accepting
          the cart - prevents a different DAO from hijacking an
          authorisation it didn't issue)
    """
    ok, reason = verify_intent_mandate(intent)
    if not ok:
        return False, f"intent: {reason}"
    ok, reason = verify_cart_mandate(cart)
    if not ok:
        return False, f"cart: {reason}"
    ok, reason = cart_satisfies_intent(cart, intent)
    if not ok:
        return False, f"cart vs intent: {reason}"
    ok, reason = verify_payment_mandate(payment)
    if not ok:
        return False, f"payment: {reason}"
    ok, reason = payment_satisfies_cart(payment, cart)
    if not ok:
        return False, f"payment vs cart: {reason}"
    # F. same DAO throughout
    intent_issuer = intent.get("issuer", "")
    payment_issuer = payment.get("issuer", "")
    if intent_issuer != payment_issuer:
        return False, (
            f"issuer mismatch: intent.issuer={intent_issuer!r} != "
            f"payment.issuer={payment_issuer!r} - a different DAO "
            "cannot hijack an authorisation"
        )
    return True, "ok"


# ===== digest + freshness =====


def payment_mandate_digest(mandate: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON minus the proof block.

    Stable across signing. Useful for receipts: a SettlementAdapter
    can include this digest in its on-chain or rail-specific receipt
    so the post-settlement audit ties back unambiguously.
    """
    payload = {k: v for k, v in mandate.items() if k != "proof"}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def is_payment_expired(
    mandate: Dict[str, Any], *, now: Optional[datetime] = None,
) -> bool:
    """True if the settlement-authority window (validUntil) has passed."""
    valid_until = mandate.get("validUntil", "")
    if not valid_until:
        return True
    try:
        deadline = datetime.fromisoformat(valid_until)
    except (ValueError, TypeError):
        return True
    if deadline.tzinfo is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current > deadline
