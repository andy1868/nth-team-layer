"""Deterministic generator for v0.10 Mandate conformance vectors (T-4).

Produces 6 vectors and patches them into ``vectors.json``:

  VALID (3) - canonical_json byte equality:
    intent_canonical    - one IntentMandate -> expected canonical bytes
    cart_canonical      - one CartMandate bound to intent_canonical
    payment_canonical   - one PaymentMandate bound to cart_canonical

  NEGATIVE (3) - validation gates must REJECT:
    cart_over_budget    - cart_satisfies_intent must fail with reason
                          containing "exceeds budget"
    payment_swap_attack - payment_satisfies_cart must fail with reason
                          containing "cart digest mismatch"
    intent_expired      - is_intent_expired with fixed `now` must
                          return True

ALL inputs are fixed (no datetime.now, no random) so a Rust / Go / TS
port can produce byte-identical canonical JSON without access to any
Python-only entropy or wall clock.

Run::

    python -m nth_dao.conformance._gen_mandate_vectors

Vectors are then asserted by the Python reference runner
(``run_all_vectors``) and locked in by test_v010_t4_mandate_conformance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..identity import canonical_json
from ..mandate.cart import cart_mandate_digest
from ..mandate.intent import intent_mandate_digest


VECTORS_PATH = Path(__file__).parent / "vectors.json"


# Pinned DIDs - these are valid base58btc did:keys whose pubkeys we never
# need to actually load. Conformance only checks canonical-JSON byte
# equality + the rejection reasons, NOT signature verification (that's
# already exercised by the signature_verify category for arbitrary keys).
DID_DAO = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSrugzCmQyDmgT1jBdU"
DID_AGENT = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
DID_SELLER = "did:key:z6MkrJVnaZkeFzdQOGOLLn4M5dHk1aoeAfBLi5XSpVPK7Lf7"

# Pinned timestamps - all UTC ISO-8601 with explicit +00:00 suffix.
TS_INTENT_ISSUED = "2026-06-01T00:00:00+00:00"
TS_INTENT_EXPIRES = "2026-06-08T00:00:00+00:00"
TS_CART_ISSUED = "2026-06-01T01:00:00+00:00"
TS_CART_EXPIRES = "2026-06-01T02:00:00+00:00"
TS_PAYMENT_ISSUED = "2026-06-01T01:30:00+00:00"
TS_PAYMENT_EXPIRES = "2026-06-01T01:45:00+00:00"

# A fixed `now` after the expired intent's validUntil - used by the
# intent_expired negative vector.
NOW_AFTER_EXPIRY = "2026-07-01T00:00:00+00:00"

# Fixed UUID4-shaped identifiers without hyphens. They are deterministic
# for conformance, but match the 32-hex wire shape produced by the live
# mandate builders.
INTENT_ID = "a1b2c3d4e5f64718a9b0c1d2e3f40516"
CART_ID = "b1c2d3e4f5064728b9c0d1e2f3041526"
PAYMENT_ID = "c1d2e3f40506472899d0e1f203142536"


# ===== Vector 1: IntentMandate canonical_json =====


INTENT_INPUT: Dict[str, Any] = {
    "@context": [
        "https://www.w3.org/ns/credentials/v2",
        "https://nth-dao.org/credentials/intent-mandate/v1",
    ],
    "type": ["VerifiableCredential", "IntentMandate"],
    "issuer": DID_DAO,
    "issuanceDate": TS_INTENT_ISSUED,
    "validFrom": TS_INTENT_ISSUED,
    "validUntil": TS_INTENT_EXPIRES,
    "credentialSubject": {
        "id": DID_AGENT,
        "intent_id": INTENT_ID,
        "purpose": "buy code review",
        "constraints": {
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [DID_SELLER],
            "allowed_settlement_methods": ["x402:usdc"],
        },
    },
}


# ===== Vector 2: CartMandate canonical_json (bound to V1) =====


def _make_cart_input() -> Dict[str, Any]:
    return {
        "@context": [
            "https://www.w3.org/ns/credentials/v2",
            "https://nth-dao.org/credentials/cart-mandate/v1",
        ],
        "type": ["VerifiableCredential", "CartMandate"],
        "issuer": DID_SELLER,
        "issuanceDate": TS_CART_ISSUED,
        "validFrom": TS_CART_ISSUED,
        "validUntil": TS_CART_EXPIRES,
        "credentialSubject": {
            "id": DID_AGENT,
            "cart_id": CART_ID,
            "intent_mandate_digest": intent_mandate_digest(INTENT_INPUT),
            "items": [
                {"description": "Code review of PR #42", "quantity": 1},
            ],
            "total": {"value": "50.00", "currency": "USDC"},
            "settlement_methods": ["x402:usdc"],
        },
    }


# ===== Vector 3: PaymentMandate canonical_json (bound to V2) =====


def _make_payment_input() -> Dict[str, Any]:
    cart = _make_cart_input()
    return {
        "@context": [
            "https://www.w3.org/ns/credentials/v2",
            "https://nth-dao.org/credentials/payment-mandate/v1",
        ],
        "type": ["VerifiableCredential", "PaymentMandate"],
        "issuer": DID_DAO,
        "issuanceDate": TS_PAYMENT_ISSUED,
        "validFrom": TS_PAYMENT_ISSUED,
        "validUntil": TS_PAYMENT_EXPIRES,
        "credentialSubject": {
            "id": DID_SELLER,
            "payment_id": PAYMENT_ID,
            "cart_mandate_digest": cart_mandate_digest(cart),
            "settlement_choice": "x402:usdc",
        },
    }


# ===== Vector 4: cart_over_budget (negative) =====


def _make_over_budget_pair() -> Dict[str, Any]:
    """A cart that claims a total ABOVE the intent's max_amount.
    cart_satisfies_intent must return ok=False with reason containing
    'exceeds budget'."""
    intent = INTENT_INPUT
    cart = _make_cart_input()
    cart["credentialSubject"]["total"] = {"value": "9999.00", "currency": "USDC"}
    # The cart is still digest-bound to the SAME intent (the binding
    # field is unchanged), so the failure must come from the amount
    # check, not the digest check.
    return {"intent": intent, "cart": cart}


# ===== Vector 5: payment_swap_attack (negative) =====


def _make_swap_attack_pair() -> Dict[str, Any]:
    """A payment that claims to bind to cart_A but is presented paired
    with cart_B at settlement time. payment_satisfies_cart must return
    ok=False with reason containing 'cart digest mismatch'."""
    cart_a = _make_cart_input()
    # Cart B: same shape, different total -> different digest
    cart_b = _make_cart_input()
    cart_b["credentialSubject"]["total"] = {"value": "99.00", "currency": "USDC"}
    # Payment is bound to cart_A's digest
    payment_for_a = _make_payment_input()
    payment_for_a["credentialSubject"]["cart_mandate_digest"] = cart_mandate_digest(cart_a)
    return {"payment": payment_for_a, "cart_presented": cart_b}


# ===== Vector 6: intent_expired (negative) =====


def _make_expired_input() -> Dict[str, Any]:
    """An intent whose validUntil has passed by the given `now`.
    is_intent_expired must return True."""
    expired = json.loads(json.dumps(INTENT_INPUT))   # deep copy
    expired["validUntil"] = "2025-01-01T00:00:00+00:00"   # year in the past
    return {"intent": expired, "now": NOW_AFTER_EXPIRY}


# ===== writer =====


def build_mandate_vectors() -> Dict[str, Any]:
    """Return the {category: [vector, ...]} dict for the mandate
    categories, with all expected outputs pre-computed."""
    cart_input = _make_cart_input()
    payment_input = _make_payment_input()
    swap = _make_swap_attack_pair()
    return {
        "mandate_intent_canonical": [{
            "id": "intent-canonical-001",
            "description": "v0.10 IntentMandate canonical JSON byte equality",
            "input": INTENT_INPUT,
            "expected_bytes_hex": canonical_json(INTENT_INPUT).hex(),
        }],
        "mandate_cart_canonical": [{
            "id": "cart-canonical-001",
            "description": "v0.10 CartMandate canonical JSON byte equality, bound to intent-canonical-001",
            "input": cart_input,
            "expected_bytes_hex": canonical_json(cart_input).hex(),
        }],
        "mandate_payment_canonical": [{
            "id": "payment-canonical-001",
            "description": "v0.10 PaymentMandate canonical JSON byte equality, bound to cart-canonical-001",
            "input": payment_input,
            "expected_bytes_hex": canonical_json(payment_input).hex(),
        }],
        "mandate_negative_binding": [
            {
                "id": "cart-over-budget",
                "description": "cart total 9999 > intent max_amount 100 -> reject",
                "input": _make_over_budget_pair(),
                "expected_ok": False,
                "expected_reason_contains": "exceeds budget",
            },
            {
                "id": "payment-swap-attack",
                "description": "payment bound to cart_A but presented with cart_B -> reject",
                "input": swap,
                "expected_ok": False,
                "expected_reason_contains": "cart digest mismatch",
            },
        ],
        "mandate_negative_expiry": [{
            "id": "intent-expired",
            "description": "intent.validUntil in 2025, now in 2026 -> is_intent_expired True",
            "input": _make_expired_input(),
            "expected_expired": True,
        }],
    }


def patch_vectors_json(path: Path = VECTORS_PATH) -> None:
    """Merge the mandate categories into vectors.json without disturbing
    pre-existing categories. Idempotent."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    doc.setdefault("vectors", {}).update(build_mandate_vectors())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    patch_vectors_json()
    print(f"patched mandate vectors into {VECTORS_PATH}")
