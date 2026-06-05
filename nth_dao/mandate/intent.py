"""IntentMandate - the AUTHORISATION half of the Mandate triad.

An IntentMandate is the DAO (or human) saying:

    "I authorise agent X to spend up to Y, with counterparties from
     this list, by these settlement methods, until this date."

It's a W3C Verifiable Credential 2.0 (data-model JSON form), signed
with Ed25519Signature2020 over canonical JSON. Shape mirrors AP2's
Intent mandate verbatim so an AP2 facilitator can consume it without
custom code.

Shape::

    {
      "@context": ["https://www.w3.org/ns/credentials/v2",
                   "https://nth-dao.org/credentials/intent-mandate/v1"],
      "type": ["VerifiableCredential", "IntentMandate"],
      "issuer": "did:key:z...",            # the authoriser
      "issuanceDate": "2026-06-...",
      "validFrom":    "2026-06-...",
      "validUntil":   "2026-06-...",
      "credentialSubject": {
        "id":      "did:key:z...",         # the AGENT being authorised
        "intent_id": "...",                # uuid; Cart/Payment bind here
        "purpose": "buy code review",
        "constraints": {
          "max_amount": {"value": "100.00", "currency": "USDC"},
          "allowed_counterparties": ["did:key:z...", ...],
          "allowed_settlement_methods": ["x402:usdc", "ap2:card"]
        }
      },
      "proof": {                            # added by sign_intent_mandate
        "type": "Ed25519Signature2020",
        "created": "2026-06-...",
        "verificationMethod": "did:key:z...#z...",
        "proofPurpose": "capabilityInvocation",
        "proofValue": "<128-hex Ed25519 sig>"
      }
    }

Why proofPurpose="capabilityInvocation"?
    Per VC Data Integrity, capabilityInvocation is the proof purpose
    for delegations/authorisations - exactly what an IntentMandate is.
    AchievementCredential uses assertionMethod because it ASSERTS facts;
    IntentMandate DELEGATES authority. Different semantics, different
    purpose value.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from ..identity import AgentIdentity, canonical_json

logger = logging.getLogger("nth_dao.mandate.intent")


INTENT_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://nth-dao.org/credentials/intent-mandate/v1",
]
INTENT_TYPE = ["VerifiableCredential", "IntentMandate"]
PROOF_TYPE = "Ed25519Signature2020"
PROOF_PURPOSE = "capabilityInvocation"

# Loose DID:key validation: prefix + base58btc'd payload. We don't fully
# decode here (did_key module owns that); we just reject obviously bad
# strings so a typo in the constraints list surfaces at build time.
_DID_KEY_RE = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]+$")

# ISO 4217 currency codes are 3 uppercase letters. We also accept "USDC"
# style 4-char stablecoin tickers; the exact set lives at the
# SettlementAdapter layer, not here.
_CURRENCY_RE = re.compile(r"^[A-Z]{3,8}$")


# ===== build =====


def build_intent_mandate(
    issuer_did: str,
    agent_did: str,
    purpose: str,
    constraints: Dict[str, Any],
    expires_at: str,
    *,
    intent_id: Optional[str] = None,
    issued_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build an UNSIGNED IntentMandate dict.

    Parameters
    ----------
    issuer_did
        did:key of the authoriser (the DAO or a human user). Must verify
        against the signing identity later in ``sign_intent_mandate``.
    agent_did
        did:key of the agent that's authorised to act under this
        mandate. Becomes ``credentialSubject.id`` per W3C VC convention.
    purpose
        Human-readable phrase describing what the agent may do
        ("buy code review", "pay for compute", "deploy fix").
    constraints
        Loose dict, validated structurally:
          max_amount: {"value": "<decimal>", "currency": "<ISO 4217>"}
            value must parse as a positive Decimal.
          allowed_counterparties: list of did:key strings, possibly
            empty (empty means: any).
          allowed_settlement_methods: list of "<adapter>:<asset>" tokens,
            possibly empty (empty means: any).
    expires_at
        ISO-8601 timestamp with timezone marker. Becomes ``validUntil``.
    intent_id
        Optional 16-hex unique id; auto-generated when omitted. Cart
        and Payment mandates will reference this.
    issued_at
        Optional issuance time; defaults to ``datetime.now(UTC)``.

    Raises
    ------
    ValueError
        On any structural validation failure - we'd rather fail at
        build time than ship a malformed mandate that a verifier
        rejects later.
    """
    if not _DID_KEY_RE.match(issuer_did):
        raise ValueError(f"issuer_did must be a did:key, got {issuer_did!r}")
    if not _DID_KEY_RE.match(agent_did):
        raise ValueError(f"agent_did must be a did:key, got {agent_did!r}")
    if not purpose or not isinstance(purpose, str):
        raise ValueError("purpose must be a non-empty string")

    validated_constraints = _validate_constraints(constraints)
    _check_iso_with_tz(expires_at, "expires_at")

    issued = (issued_at or datetime.now(timezone.utc)).isoformat()
    intent_id_value = intent_id or uuid.uuid4().hex[:16]

    return {
        "@context": list(INTENT_CONTEXT),
        "type": list(INTENT_TYPE),
        "issuer": issuer_did,
        "issuanceDate": issued,
        "validFrom": issued,
        "validUntil": expires_at,
        "credentialSubject": {
            "id": agent_did,
            "intent_id": intent_id_value,
            "purpose": purpose,
            "constraints": validated_constraints,
        },
    }


# ===== sign =====


def sign_intent_mandate(
    mandate: Dict[str, Any],
    identity: AgentIdentity,
    *,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attach Ed25519Signature2020 proof. Returns a NEW dict; input is
    not mutated.

    The identity's DID must match the mandate's issuer - we reject
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


def verify_intent_mandate(mandate: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify a signed IntentMandate.

    Returns
    -------
    (ok, reason)
        ok is True iff the credential is well-formed AND its
        Ed25519Signature2020 verifies under the issuer's did:key. On
        failure, reason names the specific check that broke.

    The two-stage shape (returns a reason, not just bool) mirrors
    AchievementCredential and verify_chain - so production code can
    log forensically actionable messages.
    """
    proof = mandate.get("proof")
    if not isinstance(proof, dict):
        return False, "missing proof"
    if proof.get("type") != PROOF_TYPE:
        return False, f"unsupported proof type: {proof.get('type')!r}"
    if proof.get("proofPurpose") != PROOF_PURPOSE:
        return False, (
            f"wrong proof purpose: {proof.get('proofPurpose')!r}; "
            f"IntentMandate requires {PROOF_PURPOSE!r}"
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


# ===== digest =====


def intent_mandate_digest(mandate: Dict[str, Any]) -> str:
    """SHA-256 over canonical JSON minus the proof block.

    Cart and Payment mandates carry this digest in their
    ``intent_mandate_digest`` field so they bind to a specific
    authorisation. Stable across signing - signing only adds a proof
    block, which is excluded from the digest by construction.
    """
    payload = {k: v for k, v in mandate.items() if k != "proof"}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


# ===== freshness =====


def is_intent_expired(
    mandate: Dict[str, Any], *, now: Optional[datetime] = None,
) -> bool:
    """True if validUntil has passed.

    A separate function from verify_intent_mandate because freshness is
    a SEPARATE concern from authenticity - an expired mandate is still
    cryptographically valid, just no longer ACTIONABLE.
    """
    valid_until = mandate.get("validUntil", "")
    if not valid_until:
        return True   # treat missing as expired (defensive)
    try:
        deadline = datetime.fromisoformat(valid_until)
    except (ValueError, TypeError):
        return True
    if deadline.tzinfo is None:
        # Naive timestamp - protocol-layer rejection
        return True
    current = now or datetime.now(timezone.utc)
    return current > deadline


# ===== helpers =====


def _validate_constraints(constraints: Any) -> Dict[str, Any]:
    """Structural validation; returns a CLEAN copy."""
    if not isinstance(constraints, dict):
        raise ValueError("constraints must be a dict")

    out: Dict[str, Any] = {}

    if "max_amount" in constraints:
        amt = constraints["max_amount"]
        if not isinstance(amt, dict):
            raise ValueError("max_amount must be a dict")
        value = amt.get("value", "")
        currency = amt.get("currency", "")
        if not isinstance(value, str):
            raise ValueError("max_amount.value must be a decimal string")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"max_amount.value is not a valid decimal: {value!r}") from exc
        if parsed <= 0:
            raise ValueError(f"max_amount.value must be positive, got {value!r}")
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            raise ValueError(f"max_amount.currency must be uppercase code, got {currency!r}")
        out["max_amount"] = {"value": value, "currency": currency}

    counterparties = constraints.get("allowed_counterparties", [])
    if not isinstance(counterparties, list):
        raise ValueError("allowed_counterparties must be a list")
    for cp in counterparties:
        if not isinstance(cp, str) or not _DID_KEY_RE.match(cp):
            raise ValueError(f"allowed_counterparties entry must be did:key, got {cp!r}")
    out["allowed_counterparties"] = list(counterparties)

    methods = constraints.get("allowed_settlement_methods", [])
    if not isinstance(methods, list):
        raise ValueError("allowed_settlement_methods must be a list")
    for m in methods:
        if not isinstance(m, str) or ":" not in m:
            raise ValueError(
                f"allowed_settlement_methods entry must be '<adapter>:<asset>', got {m!r}"
            )
    out["allowed_settlement_methods"] = list(methods)

    return out


def _check_iso_with_tz(value: str, field_name: str) -> None:
    """Parse value as ISO-8601 with explicit timezone, raise otherwise."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field_name} must carry a timezone marker (e.g. '+00:00' suffix); "
            f"naive timestamps are rejected at the protocol layer"
        )
