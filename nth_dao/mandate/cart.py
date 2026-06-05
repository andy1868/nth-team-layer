"""CartMandate - the OFFER half of the Mandate triad.

After the DAO has signed an IntentMandate (authorising agent X to spend
up to Y for purpose Z), a counterparty (the seller / service provider)
responds with a CartMandate: "here is what I'll do, for this much, on
these settlement rails, and I bind my offer to your specific intent."

The binding is via ``intent_mandate_digest`` (the IntentMandate's
canonical-JSON SHA-256 minus proof). A cart is only actionable if:

  * Its signature verifies under the cart issuer's did:key
  * Its declared digest matches the IntentMandate the DAO actually
    signed (no swap attack)
  * Its total fits inside the IntentMandate's max_amount
  * Its currency matches the IntentMandate's max_amount.currency
  * Its issuer is among the IntentMandate's allowed_counterparties
    (if that list is non-empty - empty means any)
  * At least one of its settlement_methods is among the IntentMandate's
    allowed_settlement_methods (if that list is non-empty)

The match check lives in ``cart_satisfies_intent``. Without it, the
IntentMandate's constraints are theatre - the cart would be free to
ask for ten million USDC and the audit trail would record only that
both sides signed.

Why proofPurpose=assertionMethod (not capabilityInvocation)?
    A CartMandate is the counterparty ASSERTING "I will perform X in
    exchange for Y at price Z". It's a factual claim about what they
    offer, not a delegation of authority. assertionMethod is the right
    VC Data Integrity proof purpose; verify_cart_mandate rejects the
    wrong purpose at the gate.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from ..identity import AgentIdentity, canonical_json
from .intent import (
    _DID_KEY_RE,
    _check_iso_with_tz,
    intent_mandate_digest,
)

logger = logging.getLogger("nth_dao.mandate.cart")


CART_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://nth-dao.org/credentials/cart-mandate/v1",
]
CART_TYPE = ["VerifiableCredential", "CartMandate"]
PROOF_TYPE = "Ed25519Signature2020"
PROOF_PURPOSE = "assertionMethod"


# ===== build =====


def build_cart_mandate(
    issuer_did: str,
    buyer_did: str,
    intent_mandate_digest_hex: str,
    items: List[Dict[str, Any]],
    total: Dict[str, Any],
    settlement_methods: List[str],
    expires_at: str,
    *,
    cart_id: Optional[str] = None,
    issued_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build an UNSIGNED CartMandate dict.

    Parameters
    ----------
    issuer_did
        did:key of the counterparty (the one making the offer).
    buyer_did
        did:key of the buying agent. Goes in ``credentialSubject.id``
        per W3C VC convention; should equal the agent_did named in
        the IntentMandate.
    intent_mandate_digest_hex
        SHA-256 hex of the IntentMandate this cart binds to. 64 lowercase
        hex characters; ``intent_mandate_digest()`` from .intent
        produces this for you.
    items
        Display-only line items, each a dict with at least a
        ``description``. The CANONICAL value source is ``total`` - items
        are for human audit, not pricing logic.
    total
        ``{"value": "<decimal>", "currency": "<USDC / USD / ...>"}``.
        Positive Decimal; currency uppercase short code.
    settlement_methods
        Non-empty list of ``"<adapter>:<asset>"`` tokens the counterparty
        will accept (``"x402:usdc"``, ``"ap2:card"``, etc.).
    expires_at
        ISO-8601 with timezone marker. Offer freshness window; separate
        from the IntentMandate's validUntil.
    cart_id
        Optional 16-hex unique id; auto-generated when omitted. Payment
        mandates will reference this.
    issued_at
        Optional issuance time; defaults to ``datetime.now(UTC)``.

    Raises
    ------
    ValueError
        On any structural validation failure.
    """
    if not _DID_KEY_RE.match(issuer_did):
        raise ValueError(f"issuer_did must be a did:key, got {issuer_did!r}")
    if not _DID_KEY_RE.match(buyer_did):
        raise ValueError(f"buyer_did must be a did:key, got {buyer_did!r}")
    if not isinstance(intent_mandate_digest_hex, str) or len(intent_mandate_digest_hex) != 64:
        raise ValueError(
            f"intent_mandate_digest must be a 64-hex SHA-256 string, "
            f"got {intent_mandate_digest_hex!r}"
        )
    try:
        bytes.fromhex(intent_mandate_digest_hex)
    except ValueError as exc:
        raise ValueError(
            f"intent_mandate_digest is not valid hex: {intent_mandate_digest_hex!r}"
        ) from exc

    validated_items = _validate_items(items)
    validated_total = _validate_money(total, "total")
    validated_methods = _validate_settlement_methods(settlement_methods)
    _check_iso_with_tz(expires_at, "expires_at")

    issued = (issued_at or datetime.now(timezone.utc)).isoformat()
    cart_id_value = cart_id or uuid.uuid4().hex[:16]

    return {
        "@context": list(CART_CONTEXT),
        "type": list(CART_TYPE),
        "issuer": issuer_did,
        "issuanceDate": issued,
        "validFrom": issued,
        "validUntil": expires_at,
        "credentialSubject": {
            "id": buyer_did,
            "cart_id": cart_id_value,
            "intent_mandate_digest": intent_mandate_digest_hex,
            "items": validated_items,
            "total": validated_total,
            "settlement_methods": validated_methods,
        },
    }


# ===== sign =====


def sign_cart_mandate(
    mandate: Dict[str, Any],
    identity: AgentIdentity,
    *,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attach Ed25519Signature2020 proof. Returns a NEW dict; input
    is not mutated.

    The signing identity's DID must match ``mandate.issuer`` - we reject
    cross-issuer signing at build time rather than producing a credential
    no verifier will accept.
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


def verify_cart_mandate(mandate: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify a signed CartMandate.

    Returns ``(ok, reason)``. Authenticity only - does NOT check whether
    the cart satisfies a specific IntentMandate; that's
    ``cart_satisfies_intent``. Two-stage gate so callers can:

      1. Reject unsigned / tampered carts cheaply (no intent fetch)
      2. Only when authentic, evaluate intent compatibility
    """
    proof = mandate.get("proof")
    if not isinstance(proof, dict):
        return False, "missing proof"
    if proof.get("type") != PROOF_TYPE:
        return False, f"unsupported proof type: {proof.get('type')!r}"
    if proof.get("proofPurpose") != PROOF_PURPOSE:
        return False, (
            f"wrong proof purpose: {proof.get('proofPurpose')!r}; "
            f"CartMandate requires {PROOF_PURPOSE!r}"
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


# ===== intent-cart binding check =====


def cart_satisfies_intent(
    cart: Dict[str, Any],
    intent: Dict[str, Any],
) -> Tuple[bool, str]:
    """Check that a CartMandate fits inside an IntentMandate's
    constraints.

    Returns ``(ok, reason)``. Without this check the constraints in
    IntentMandate would be theatre: the cart could ask for any total in
    any currency from any counterparty.

    Checks (short-circuit on first failure):

      1. cart.credentialSubject.intent_mandate_digest == digest(intent)
         (swap-attack defence)
      2. cart.total.currency == intent.max_amount.currency
      3. cart.total.value <= intent.max_amount.value (Decimal compare)
      4. cart.issuer in intent.allowed_counterparties (if list non-empty)
      5. at least one cart.settlement_methods in
         intent.allowed_settlement_methods (if list non-empty)

    Callers should pair this with verify_cart_mandate (which checks
    authenticity) and verify_intent_mandate (likewise) - this function
    does NOT verify signatures, only economic compatibility.
    """
    if not isinstance(cart, dict) or not isinstance(intent, dict):
        return False, "cart and intent must be dicts"

    cart_subject = cart.get("credentialSubject", {})
    if not isinstance(cart_subject, dict):
        return False, "cart.credentialSubject malformed"

    # 1. binding via digest
    declared_digest = cart_subject.get("intent_mandate_digest", "")
    actual_digest = intent_mandate_digest(intent)
    if declared_digest != actual_digest:
        return False, (
            f"intent digest mismatch: cart binds to "
            f"{declared_digest[:16]}..., intent digest is "
            f"{actual_digest[:16]}..."
        )

    intent_subject = intent.get("credentialSubject", {})
    constraints = intent_subject.get("constraints", {})
    if not isinstance(constraints, dict):
        return False, "intent.credentialSubject.constraints malformed"

    # 2 + 3. amount / currency
    cart_total = cart_subject.get("total", {})
    if not isinstance(cart_total, dict):
        return False, "cart.total malformed"
    cart_value_str = cart_total.get("value", "")
    cart_currency = cart_total.get("currency", "")
    try:
        cart_value = Decimal(cart_value_str)
    except InvalidOperation:
        return False, f"cart.total.value not a valid decimal: {cart_value_str!r}"

    max_amount = constraints.get("max_amount") or {}
    if max_amount:
        max_value_str = max_amount.get("value", "")
        max_currency = max_amount.get("currency", "")
        if cart_currency != max_currency:
            return False, (
                f"currency mismatch: cart {cart_currency!r} != "
                f"intent max_amount {max_currency!r}"
            )
        try:
            max_value = Decimal(max_value_str)
        except InvalidOperation:
            return False, f"intent.max_amount.value not a valid decimal: {max_value_str!r}"
        if cart_value > max_value:
            return False, (
                f"total exceeds budget: cart {cart_value} {cart_currency} > "
                f"intent max {max_value} {max_currency}"
            )

    # 4. counterparty allow-list
    allowed_counterparties = constraints.get("allowed_counterparties", []) or []
    if allowed_counterparties:
        cart_issuer = cart.get("issuer", "")
        if cart_issuer not in allowed_counterparties:
            return False, (
                f"counterparty {cart_issuer!r} not in intent "
                f"allowed_counterparties list"
            )

    # 5. settlement method allow-list
    allowed_methods = constraints.get("allowed_settlement_methods", []) or []
    if allowed_methods:
        cart_methods = cart_subject.get("settlement_methods", []) or []
        if not any(m in allowed_methods for m in cart_methods):
            return False, (
                f"no overlap between cart methods {cart_methods} and "
                f"intent allowed_settlement_methods {allowed_methods}"
            )

    return True, "ok"


# ===== digest + freshness =====


def cart_mandate_digest(mandate: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON minus the proof block.

    PaymentMandate (T-3) will carry this in its ``cart_mandate_digest``
    field so the payment binds to a specific cart. Stable across signing.
    """
    payload = {k: v for k, v in mandate.items() if k != "proof"}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def is_cart_expired(
    mandate: Dict[str, Any], *, now: Optional[datetime] = None,
) -> bool:
    """True if the cart's offer window (validUntil) has passed."""
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


# ===== helpers =====


def _validate_money(amount: Any, field_name: str) -> Dict[str, str]:
    """Strict positive-Decimal + uppercase-currency validation."""
    if not isinstance(amount, dict):
        raise ValueError(f"{field_name} must be a dict")
    value = amount.get("value", "")
    currency = amount.get("currency", "")
    if not isinstance(value, str):
        raise ValueError(f"{field_name}.value must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name}.value is not a valid decimal: {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name}.value must be positive, got {value!r}")
    if not isinstance(currency, str) or not currency.isupper() or not 3 <= len(currency) <= 8:
        raise ValueError(
            f"{field_name}.currency must be uppercase 3-8 char code, got {currency!r}"
        )
    return {"value": value, "currency": currency}


def _validate_items(items: Any) -> List[Dict[str, Any]]:
    """Items are display-only; minimal contract: list of dicts, each
    with at least a non-empty description."""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] must be a dict")
        description = item.get("description", "")
        if not isinstance(description, str) or not description:
            raise ValueError(f"items[{i}].description must be a non-empty string")
        out.append(dict(item))
    return out


def _validate_settlement_methods(methods: Any) -> List[str]:
    if not isinstance(methods, list) or not methods:
        raise ValueError("settlement_methods must be a non-empty list")
    for m in methods:
        if not isinstance(m, str) or ":" not in m:
            raise ValueError(
                f"settlement_methods entry must be '<adapter>:<asset>', got {m!r}"
            )
    return list(methods)
