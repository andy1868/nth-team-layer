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

import copy
import json
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

from ..event_bus import (
    MANDATE_CART_RECEIVED,
    MANDATE_INTENT_ISSUED,
    MANDATE_PAYMENT_AUTHORISED,
    SETTLEMENT_COMPLETED,
)


logger = logging.getLogger("nth_dao.mandate.events")

# Voss V-44: hard cap on the serialized size of a single event
# payload. The EventBus stores events as JSON lines; an attacker
# (or a buggy adapter) feeding a 10 MB `total` field would inflate
# the audit log + slow replay to a crawl. 64 KB is generous for any
# legitimate Mandate audit payload while keeping per-event storage
# bounded.
_EVENT_PAYLOAD_MAX_BYTES = 65536


def _check_event_payload_size(event_type: str, payload: Dict[str, Any]) -> None:
    """Raise ValueError if ``payload`` serializes past the cap."""
    try:
        size = len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{event_type} payload contains non-JSON-serialisable values: {exc}"
        ) from exc
    if size > _EVENT_PAYLOAD_MAX_BYTES:
        raise ValueError(
            f"{event_type} payload exceeds {_EVENT_PAYLOAD_MAX_BYTES} bytes "
            f"(got {size}); audit log entries must stay bounded"
        )
from ..util import now_iso
from .cart import cart_mandate_digest, verify_cart_mandate
from .intent import intent_mandate_digest, verify_intent_mandate
from .payment import payment_mandate_digest, verify_payment_mandate

if TYPE_CHECKING:
    from ..event_bus import BusEvent, EventBus
    from ..identity import AgentIdentity


# ===== mandate.intent.issued =====


def emit_intent_issued(
    bus: "EventBus",
    intent_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
    require_signed: bool = True,
) -> "BusEvent":
    """Emit a `mandate.intent.issued` event for the given IntentMandate.

    Extracts the digest + filterable summary; does NOT include the full
    Mandate (which lives in whatever store the issuer keeps).

    By default (``require_signed=True``) refuses to emit for an
    unsigned or invalidly-signed IntentMandate (Voss V-34). The whole
    point of the audit bus is that "this event happened" can be
    trusted - letting any caller emit `mandate.intent.issued` for an
    unsigned dict turns the audit chain into pseudo-evidence.

    Set ``require_signed=False`` only in tests that exercise the
    emit-path without the cost of signing fixtures.

    Raises
    ------
    ValueError
        If ``intent_mandate`` is missing required fields or fails the
        signature gate.
    """
    if require_signed:
        ok, reason = verify_intent_mandate(intent_mandate)
        if not ok:
            raise ValueError(
                f"refuse to emit mandate.intent.issued for invalid intent: "
                f"{reason}"
            )
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
    # Voss V-49: deepcopy so a caller mutating the source mandate
    # after emit doesn't retroactively rewrite the event payload.
    max_amount = copy.deepcopy(constraints.get("max_amount")) or None
    payload: Dict[str, Any] = {
        "intent_id": intent_id,
        "intent_mandate_digest": intent_mandate_digest(intent_mandate),
        "issuer": issuer,
        "agent_did": agent_did,
        "purpose": purpose,
        "max_amount": max_amount,
        "expires_at": intent_mandate.get("validUntil", ""),
    }
    _check_event_payload_size(MANDATE_INTENT_ISSUED, payload)
    return bus.emit(MANDATE_INTENT_ISSUED, payload, identity=identity)


# ===== mandate.cart.received =====


def emit_cart_received(
    bus: "EventBus",
    cart_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
    require_signed: bool = True,
) -> "BusEvent":
    """Emit a `mandate.cart.received` event for the given CartMandate.

    Voss V-34: default verifies the cart's signature before emitting.
    See ``emit_intent_issued`` for the rationale.
    """
    if require_signed:
        ok, reason = verify_cart_mandate(cart_mandate)
        if not ok:
            raise ValueError(
                f"refuse to emit mandate.cart.received for invalid cart: "
                f"{reason}"
            )
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
        # V-49: deepcopy nested objects so caller mutation can't
        # retroactively rewrite the event payload.
        "total": copy.deepcopy(subject.get("total", {})),
        "settlement_methods": list(subject.get("settlement_methods", []) or []),
        "expires_at": cart_mandate.get("validUntil", ""),
    }
    _check_event_payload_size(MANDATE_CART_RECEIVED, payload)
    return bus.emit(MANDATE_CART_RECEIVED, payload, identity=identity)


# ===== mandate.payment.authorised =====


def emit_payment_authorised(
    bus: "EventBus",
    payment_mandate: Dict[str, Any],
    *,
    identity: Optional["AgentIdentity"] = None,
    require_signed: bool = True,
) -> "BusEvent":
    """Emit a `mandate.payment.authorised` event for the given PaymentMandate.

    Voss V-34: default verifies the payment's signature before
    emitting. See ``emit_intent_issued`` for the rationale.
    """
    if require_signed:
        ok, reason = verify_payment_mandate(payment_mandate)
        if not ok:
            raise ValueError(
                f"refuse to emit mandate.payment.authorised for invalid "
                f"payment: {reason}"
            )
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
    _check_event_payload_size(MANDATE_PAYMENT_AUTHORISED, payload)
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
    require_prior_authorisation: bool = True,
) -> "BusEvent":
    """Emit a `settlement.completed` event after a SettlementAdapter
    finishes attempting an external-rail transaction.

    Unlike the three Mandate emit helpers, this one does NOT take a
    Mandate dict - by this point the chain is already on EventBus and
    only the digest + adapter outcome are needed for audit. The receipt
    is whatever the adapter knows about (tx hash, ACH reference, etc.)
    and its shape is OWNED by the adapter, not this module.

    Voss V-34 audit-chain integrity (this revision):
        By default (``require_prior_authorisation=True``) the function
        scans the EventBus for a prior ``mandate.payment.authorised``
        event whose ``payment_mandate_digest`` matches the digest
        passed here. If none is found, the emit is refused. Without
        this gate, any code path can pollute the audit chain with a
        fabricated ``settlement.completed`` for arbitrary digests,
        making forensic replay untrustworthy.

        Scan is O(N) in the size of the lifecycle event stream
        (filtered by event_type, so cheap). For very high-volume
        deployments swap this for an indexed lookup in a follow-up.

        Set ``require_prior_authorisation=False`` only for migration
        scenarios or in tests that exercise the payload-shape branch
        without first emitting the full chain. Production code paths
        should NEVER pass False.

    Raises
    ------
    ValueError
        If ``payment_mandate_digest_hex`` is not 64 hex characters,
        ``outcome`` is not ``"success"`` or ``"failure"``, OR (with
        ``require_prior_authorisation=True``) no prior
        ``mandate.payment.authorised`` carries the same digest.
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

    # Voss V-34 + F-3 (4th-round): audit-chain integrity check.
    # Must find a prior mandate.payment.authorised AND the
    # settlement_choice in this completion must match the choice
    # the DAO authorised. Otherwise a rogue adapter could "complete"
    # via a rail the DAO never approved while the audit trail still
    # shows the consistent authorise+complete pair.
    if require_prior_authorisation:
        prior = _find_prior_authorisation(bus, payment_mandate_digest_hex)
        if prior is None:
            raise ValueError(
                f"refuse to emit settlement.completed: no prior "
                f"mandate.payment.authorised event found on the bus "
                f"for digest {payment_mandate_digest_hex[:16]}... "
                f"(pass require_prior_authorisation=False ONLY for "
                f"migration scenarios)"
            )
        authorised_choice = prior.get("settlement_choice", "")
        if authorised_choice != settlement_choice:
            raise ValueError(
                f"settlement_choice {settlement_choice!r} does not "
                f"match the authorised choice {authorised_choice!r} "
                f"from the prior mandate.payment.authorised event "
                f"(digest {payment_mandate_digest_hex[:16]}...). A "
                f"SettlementAdapter cannot complete via a rail the "
                f"DAO did not authorise."
            )
    elif not require_prior_authorisation:
        # F-6: leave a forensic trace whenever the bypass is used.
        logger.warning(
            "settlement.completed emitted WITHOUT prior-authorisation "
            "check for digest %s... (require_prior_authorisation=False; "
            "production code must not pass this flag)",
            payment_mandate_digest_hex[:16],
        )

    payload: Dict[str, Any] = {
        "payment_mandate_digest": payment_mandate_digest_hex,
        "adapter": adapter,
        "settlement_choice": settlement_choice,
        "outcome": outcome,
        # V-49: deepcopy the adapter-supplied receipt so a buggy
        # adapter mutating its receipt dict after emit can't rewrite
        # the audit entry.
        "receipt": copy.deepcopy(receipt) if receipt else {},
        "completed_at": completed_at or now_iso(),
    }
    _check_event_payload_size(SETTLEMENT_COMPLETED, payload)
    return bus.emit(SETTLEMENT_COMPLETED, payload, identity=identity)


def _find_prior_authorisation(
    bus: "EventBus", payment_mandate_digest_hex: str,
) -> Optional[Dict[str, Any]]:
    """Scan the EventBus for a prior mandate.payment.authorised event
    matching the digest, return its payload (or None).

    Walks the stream in REVERSE (newest first) on the assumption
    that settlement.completed normally follows soon after the
    authorisation it corresponds to. Returns the FULL payload of
    the matching event so callers can cross-check additional fields
    (e.g. F-3 settlement_choice consistency).
    """
    for event in bus.replay(
        event_types=[MANDATE_PAYMENT_AUTHORISED], reverse=True,
    ):
        payload = getattr(event, "payload", None) or {}
        if payload.get("payment_mandate_digest") == payment_mandate_digest_hex:
            return payload
    return None


def _payment_was_authorised_on_bus(
    bus: "EventBus", payment_mandate_digest_hex: str,
) -> bool:
    """Back-compat wrapper kept for any external caller. Prefer
    ``_find_prior_authorisation`` which returns the full payload."""
    return _find_prior_authorisation(bus, payment_mandate_digest_hex) is not None


__all__ = [
    "emit_intent_issued",
    "emit_cart_received",
    "emit_payment_authorised",
    "emit_settlement_completed",
]
