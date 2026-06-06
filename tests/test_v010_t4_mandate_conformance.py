"""T-4 conformance vectors for v0.10 Mandate primitives.

vectors.json is the cross-implementation contract: a Rust / Go / TS
port of NTH DAO is considered wire-compatible iff running the
equivalent of run_all_vectors() under that language produces zero
failures.

T-4 adds 6 vectors covering the Mandate triad:

  VALID (canonical-JSON byte equality, 3):
    mandate_intent_canonical    intent-canonical-001
    mandate_cart_canonical      cart-canonical-001 (bound to V1)
    mandate_payment_canonical   payment-canonical-001 (bound to V2)

  NEGATIVE (gate must REJECT with specific reason, 3):
    mandate_negative_binding    cart-over-budget       -> "exceeds budget"
    mandate_negative_binding    payment-swap-attack    -> "cart digest mismatch"
    mandate_negative_expiry     intent-expired         -> True

Tests:
  * Vector inventory matches the spec (correct categories, correct ids,
    correct counts).
  * Runner reports zero failures for the mandate categories.
  * Vectors are byte-stable across regeneration (regenerator is
    deterministic; running it twice must produce identical bytes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nth_dao.conformance import run_all_vectors
from nth_dao.conformance._gen_mandate_vectors import (
    VECTORS_PATH,
    build_mandate_vectors,
)


def _load_vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


# ===== inventory =====


def test_T4_all_six_vectors_present():
    """The vectors.json shipped to consumers MUST contain exactly the
    six T-4 vectors under the documented category names."""
    doc = _load_vectors()
    cats = doc["vectors"]

    # Three canonical-JSON categories with one vector each
    assert len(cats["mandate_intent_canonical"]) == 1
    assert cats["mandate_intent_canonical"][0]["id"] == "intent-canonical-001"
    assert len(cats["mandate_cart_canonical"]) == 1
    assert cats["mandate_cart_canonical"][0]["id"] == "cart-canonical-001"
    assert len(cats["mandate_payment_canonical"]) == 1
    assert cats["mandate_payment_canonical"][0]["id"] == "payment-canonical-001"

    # Two negative binding vectors
    binding = cats["mandate_negative_binding"]
    assert len(binding) == 2
    ids = sorted(v["id"] for v in binding)
    assert ids == ["cart-over-budget", "payment-swap-attack"]

    # One negative expiry vector
    expiry = cats["mandate_negative_expiry"]
    assert len(expiry) == 1
    assert expiry[0]["id"] == "intent-expired"


def test_T4_total_count_is_six():
    doc = _load_vectors()
    cats = doc["vectors"]
    total = sum(
        len(cats[c])
        for c in (
            "mandate_intent_canonical",
            "mandate_cart_canonical",
            "mandate_payment_canonical",
            "mandate_negative_binding",
            "mandate_negative_expiry",
        )
    )
    assert total == 6   # the spec said six; we ship six


# ===== runner =====


def test_T4_runner_reports_zero_failures_on_mandate_categories():
    """The reference (Python) implementation MUST pass every mandate
    vector. A non-Python port is wire-compatible iff their runner does
    likewise."""
    failures = run_all_vectors()
    mandate_failures = [
        f for f in failures
        if f.category.startswith("mandate_")
    ]
    assert mandate_failures == [], (
        "mandate conformance failures:\n"
        + "\n".join(
            f"  {f.category}/{f.vector_id}: expected={f.expected!r} "
            f"actual={f.actual!r}"
            for f in mandate_failures
        )
    )


# ===== determinism =====


def test_T4_regenerator_is_deterministic():
    """Running the generator twice must produce byte-identical output.
    Without this, any port doing a 'fresh generation' from the same
    inputs could produce different bytes than our shipped vectors."""
    first = build_mandate_vectors()
    second = build_mandate_vectors()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_T4_canonical_bytes_match_freshly_computed():
    """Each canonical_json vector's expected_bytes_hex must equal what
    canonical_json produces for the vector's input right now. This
    catches drift between vectors.json and the live encoder."""
    from nth_dao.identity import canonical_json

    doc = _load_vectors()
    for cat in (
        "mandate_intent_canonical",
        "mandate_cart_canonical",
        "mandate_payment_canonical",
    ):
        for v in doc["vectors"][cat]:
            fresh = canonical_json(v["input"]).hex()
            assert fresh == v["expected_bytes_hex"], (
                f"{cat}/{v['id']}: vectors.json is stale; regenerate via "
                f"`python -m nth_dao.conformance._gen_mandate_vectors`"
            )


def test_T4_mandate_ids_are_full_uuid4_hex_shape():
    """Mandate IDs in conformance vectors must match the public wire
    shape: 32 lowercase hex chars with UUID4 version/variant nibbles.
    A 16-hex fixture would let non-Python ports accidentally implement
    the wrong ID width."""
    doc = _load_vectors()
    fields = (
        ("mandate_intent_canonical", "intent_id"),
        ("mandate_cart_canonical", "cart_id"),
        ("mandate_payment_canonical", "payment_id"),
    )
    for category, field in fields:
        value = doc["vectors"][category][0]["input"]["credentialSubject"][field]
        assert len(value) == 32
        assert value.lower() == value
        assert all(ch in "0123456789abcdef" for ch in value)
        assert value[12] == "4"
        assert value[16] in "89ab"


# ===== binding semantics =====


def test_T4_negative_binding_vectors_match_their_documented_reasons():
    """Spot-check that the rejection reason in vectors.json corresponds
    to the actual reason string the binding checks return. If our gate
    changes its wording, this test makes the conformance drift visible."""
    from nth_dao.mandate.cart import cart_satisfies_intent
    from nth_dao.mandate.payment import payment_satisfies_cart

    doc = _load_vectors()
    for v in doc["vectors"]["mandate_negative_binding"]:
        inp = v["input"]
        # Vectors are structural fixtures (no proof block); pass
        # require_signed=False so V-21's signature gate doesn't
        # short-circuit the structural reason we're pinning here.
        if "cart" in inp and "intent" in inp:
            ok, reason = cart_satisfies_intent(
                inp["cart"], inp["intent"], require_signed=False,
            )
        else:
            ok, reason = payment_satisfies_cart(
                inp["payment"], inp["cart_presented"], require_signed=False,
            )
        assert ok is False
        assert v["expected_reason_contains"] in reason, (
            f"vector {v['id']}: reason='{reason}' did not contain "
            f"documented substring {v['expected_reason_contains']!r}"
        )
