"""Mandate-lifecycle EventBus emit helpers (v0.10 T-5).

Subsystems that participate in the Mandate triad (intent registry,
cart receiver, payment processor, settlement adapter) MUST emit their
lifecycle audit signals via these helpers rather than calling
``bus.emit`` with hand-rolled payloads.

Reasons:
  * The event_type strings live in event_bus.py as constants
    (MANDATE_INTENT_ISSUED, etc.). Helpers use those constants directly,
    so typos at the call site become AttributeError instead of silent
    audit-trail divergence.
  * Each helper extracts a SMALL, FILTERABLE payload from the source
    mandate (digest + key fields) rather than embedding the whole
    Mandate. Downstream consumers (UI, settlement adapters) can grep by
    ``intent_id`` / ``cart_id`` / ``payment_id`` without reading the
    Mandate body.
  * Structural validation at emit time - if a subsystem passes a
    malformed Mandate dict, ValueError fires here, not silently
    elsewhere days later.

Replaying the lifecycle stream::

    from nth_dao.event_bus import MANDATE_LIFECYCLE_EVENT_TYPES
    for ev in bus.replay(event_types=list(MANDATE_LIFECYCLE_EVENT_TYPES)):
        process(ev)

That's the documented contract for any consumer that wants the full
audit chain for the AP2-shape Mandate triad.

Payload shapes (each is the canonical event.payload dict):

  mandate.intent.issued::
    {
      "intent_id":              16-hex,
      "intent_mandate_digest":  64-hex SHA-256 of canonical_json
                                (excludes proof),
      "issuer":                 did:key (DAO),
      "agent_did":              did:key (authorised agent),
      "purpose":                str,
      "max_amount":             {"value": str, "currency": str} | None,
      "expires_at":             ISO-8601 UTC,
    }

  mandate.cart.received::
    {
      "cart_id":                16-hex,
      "cart_mandate_digest":    64-hex,
      "intent_mandate_digest":  64-hex - the binding to V1,
      "issuer":                 did:key (counterparty / seller),
      "buyer_did":              did:key (= intent.agent_did),
      "total":                  {"value": str, "currency": str},
      "settlement_methods":     ["x402:usdc", ...],
      "expires_at":             ISO-8601 UTC,
    }

  mandate.payment.authorised::
    {
      "payment_id":             16-hex,
      "payment_mandate_digest": 64-hex,
      "cart_mandate_digest":    64-hex - the binding to V2,
      "issuer":                 did:key (DAO, same as intent.issuer),
      "payee_did":              did:key (= cart.issuer),
      "settlement_choice":      "x402:usdc",
      "expires_at":             ISO-8601 UTC,
    }

  settlement.completed::
    {
      "payment_mandate_digest": 64-hex - binds the receipt back to V3,
      "adapter":                "x402" | "ap2_card" | "manual" | ...,
      "settlement_choice":      "x402:usdc" (denormalised for cheap filter),
      "outcome":                "success" | "failure",
      "receipt":                dict (adapter-specific shape - tx_hash for
                                x402, ACH reference for AP2, ...),
      "completed_at":           ISO-8601 UTC,
    }
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from ..event_bus import (
    MANDATE_CART_RECEIVED,
    MANDATE_INTENT_ISSUED,
    MANDATE_PAYMENT_AUTHORISED,
    SETTLEMENT_COMPLETED,
)
from ..util import now_iso
from .cart import cart_mandate_digest
from .intent import intent_mandate_digest
from .payment import payment_mandate_digest

if TYPE_CHECKING:
    from ..event_bus import BusEvent, EventBus
    from ..identity import AgentIdentity


# ===== mandate.intent.issued =====


def emit_intent_issued(
    bus: "EventBus",
    intent_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
) -> "BusEvent":
    """Emit a `mandate.intent.issued` event for the given IntentMandate.

    Extracts the digest + filterable summary; does NOT include the full
    Mandate (which lives in whatever store the issuer keeps).

    Raises
    ------
    ValueError
        If ``intent_mandate`` is missing required fields. Catches bugs
        early - the audit trail must not be polluted with malformed
        payloads.
    """
    subject = intent_mandate.get("credentialSubject", {})
    if not isinstance(subject, dict):
        raise ValueError("intent_mandate.credentialSubject must be a dict")
    intent_id = subject.get("intent_id", "")
    issuer = intent_mandate.get("issuer", "")
    agent_did = subject.get("id", "")
    purpose = subject.get("purpose", "")
    if not intent_id or not issuer or not agent_did:
        raise ValueError(
            "intent_mandate is missing required fields: "
            "credentialSubject.intent_id / issuer / credentialSubject.id"
        )
    constraints = subject.get("constraints", {}) or {}
    max_amount = constraints.get("max_amount") or None
    payload: Dict[str, Any] = {
        "intent_id": intent_id,
        "intent_mandate_digest": intent_mandate_digest(intent_mandate),
        "issuer": issuer,
        "agent_did": agent_did,
        "purpose": purpose,
        "max_amount": max_amount,
        "expires_at": intent_mandate.get("validUntil", ""),
    }
    return bus.emit(MANDATE_INTENT_ISSUED, payload, identity=identity)


# ===== mandate.cart.received =====


def emit_cart_received(
    bus: "EventBus",
    cart_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
) -> "BusEvent":
    """Emit a `mandate.cart.received` event for the given CartMandate."""
    subject = cart_mandate.get("credentialSubject", {})
    if not isinstance(subject, dict):
        raise ValueError("cart_mandate.credentialSubject must be a dict")
    cart_id = subject.get("cart_id", "")
    intent_digest = subject.get("intent_mandate_digest", "")
    issuer = cart_mandate.get("issuer", "")
    buyer_did = subject.get("id", "")
    if not cart_id or not intent_digest or not issuer or not buyer_did:
        raise ValueError(
            "cart_mandate is missing required fields: "
            "credentialSubject.cart_id / intent_mandate_digest / "
            "issuer / credentialSubject.id"
        )
    payload: Dict[str, Any] = {
        "cart_id": cart_id,
        "cart_mandate_digest": cart_mandate_digest(cart_mandate),
        "intent_mandate_digest": intent_digest,
        "issuer": issuer,
        "buyer_did": buyer_did,
        "total": subject.get("total", {}),
        "settlement_methods": list(subject.get("settlement_methods", []) or []),
        "expires_at": cart_mandate.get("validUntil", ""),
    }
    return bus.emit(MANDATE_CART_RECEIVED, payload, identity=identity)


# ===== mandate.payment.authorised =====


def emit_payment_authorised(
    bus: "EventBus",
    payment_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
) -> "BusEvent":
    """Emit a `mandate.payment.authorised` event for the given PaymentMandate."""
    subject = payment_mandate.get("credentialSubject", {})
    if not isinstance(subject, dict):
        raise ValueError("payment_mandate.credentialSubject must be a dict")
    payment_id = subject.get("payment_id", "")
    cart_digest = subject.get("cart_mandate_digest", "")
    issuer = payment_mandate.get("issuer", "")
    payee_did = subject.get("id", "")
    choice = subject.get("settlement_choice", "")
    if not payment_id or not cart_digest or not issuer or not payee_did or not choice:
        raise ValueError(
            "payment_mandate is missing required fields: "
            "credentialSubject.payment_id / cart_mandate_digest / "
            "issuer / credentialSubject.id / settlement_choice"
        )
    payload: Dict[str, Any] = {
        "payment_id": payment_id,
        "payment_mandate_digest": payment_mandate_digest(payment_mandate),
        "cart_mandate_digest": cart_digest,
        "issuer": issuer,
        "payee_did": payee_did,
        "settlement_choice": choice,
        "expires_at": payment_mandate.get("validUntil", ""),
    }
    return bus.emit(MANDATE_PAYMENT_AUTHORISED, payload, identity=identity)


# ===== settlement.completed =====


def emit_settlement_completed(
    bus: "EventBus",
    *,
    payment_mandate_digest_hex: str,
    adapter: str,
    settlement_choice: str,
    outcome: str,
    receipt: Optional[Dict[str, Any]] = None,
    completed_at: Optional[str] = None,
    identity: Optional["AgentIdentity"] = None,
) -> "BusEvent":
    """Emit a `settlement.completed` event after a SettlementAdapter
    finishes attempting an external-rail transaction.

    Unlike the three Mandate emit helpers, this one does NOT take a
    Mandate dict - by this point the chain is already on EventBus and
    only the digest + adapter outcome are needed for audit. The receipt
    is whatever the adapter knows about (tx hash, ACH reference, etc.)
    and its shape is OWNED by the adapter, not this module.

    Raises
    ------
    ValueError
        If ``payment_mandate_digest_hex`` is not 64 hex characters or
        ``outcome`` is not ``"success"`` or ``"failure"``. These are
        the only enforced contracts; the receipt is free-form.
    """
    if not isinstance(payment_mandate_digest_hex, str) or len(payment_mandate_digest_hex) != 64:
        raise ValueError(
            f"payment_mandate_digest must be 64-hex, got "
            f"{payment_mandate_digest_hex!r}"
        )
    try:
        bytes.fromhex(payment_mandate_digest_hex)
    except ValueError as exc:
        raise ValueError(
            f"payment_mandate_digest is not valid hex: "
            f"{payment_mandate_digest_hex!r}"
        ) from exc
    if outcome not in ("success", "failure"):
        raise ValueError(
            f"outcome must be 'success' or 'failure', got {outcome!r}"
        )
    if not adapter or not isinstance(adapter, str):
        raise ValueError("adapter must be a non-empty string")
    if not settlement_choice or ":" not in settlement_choice:
        raise ValueError(
            f"settlement_choice must be '<adapter>:<asset>', "
            f"got {settlement_choice!r}"
        )

    payload: Dict[str, Any] = {
        "payment_mandate_digest": payment_mandate_digest_hex,
        "adapter": adapter,
        "settlement_choice": settlement_choice,
        "outcome": outcome,
        "receipt": dict(receipt) if receipt else {},
        "completed_at": completed_at or now_iso(),
    }
    return bus.emit(SETTLEMENT_COMPLETED, payload, identity=identity)


__all__ = [
    "emit_intent_issued",
    "emit_cart_received",
    "emit_payment_authorised",
    "emit_settlement_completed",
]
