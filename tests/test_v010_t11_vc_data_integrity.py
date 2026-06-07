"""T-1.1: W3C VC Data Integrity §4 conformance + did:key multibase fragment.

Covers Voss V-1 and V-9 across all three mandates (Intent, Cart,
Payment). These were deferred from the first Voss round because they
required a coordinated signing rewrite. Now landed.

The promise of this revision:

  1. The signature covers BOTH the document AND the proof options
     (everything in the proof block except proofValue). Tampering
     proof.created, proof.proofPurpose, or proof.verificationMethod
     after signing must invalidate the signature.

  2. verificationMethod fragment is the multibase z-string from the
     did:key body, not the raw pubkey hex. Format:

       did:key:z6MkXyz...#z6MkXyz...

     Any VC Data Integrity verifier (didkit, vc-js, Universal
     Resolver) parses the fragment as the key identifier within
     the issuer's DID Document. The previous raw-hex format was
     non-conformant.

  3. The three mandates' sign+verify share an internal
     `_data_integrity.py` helper so future spec updates land in one
     place, not three.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate._data_integrity import (
    sign_with_data_integrity,
    verification_method,
    verify_with_data_integrity,
)
from nth_dao.mandate.cart import (
    build_cart_mandate,
    sign_cart_mandate,
    verify_cart_mandate,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
    verify_intent_mandate,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    sign_payment_mandate,
    verify_payment_mandate,
    cart_mandate_digest,
)


# ===== shared fixtures =====


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t11-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t11-seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t11-agent").as_did()


def _future(s: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()


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


def _signed_cart(seller, agent_did, intent_digest) -> Dict[str, Any]:
    c = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    )
    return sign_cart_mandate(c, seller)


def _signed_payment(dao, seller, cart_digest) -> Dict[str, Any]:
    p = build_payment_mandate(
        issuer_did=dao.as_did(), payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice="x402:usdc", expires_at=_future(900),
    )
    return sign_payment_mandate(p, dao)


# =====================================================================
# V-9: verificationMethod fragment shape
# =====================================================================


def test_T11_V9_verification_method_uses_multibase_fragment(dao, agent_did):
    """Fragment must be the did:key body, not the raw pubkey hex.
    For ``did:key:z6Mk...`` the verificationMethod is
    ``did:key:z6Mk...#z6Mk...`` per did:key spec §3.1."""
    intent = _signed_intent(dao, agent_did)
    vm = intent["proof"]["verificationMethod"]
    dao_did = dao.as_did()
    # Shape: <issuer_did>#<multibase_z_string>
    prefix, _, fragment = vm.partition("#")
    assert prefix == dao_did
    # Fragment should be the issuer's z-string body (matches DID body)
    expected_fragment = dao_did.split(":", 2)[-1]
    assert fragment == expected_fragment
    # And it should NOT be the raw 64-hex pubkey
    assert fragment != dao.pubkey_hex
    assert len(fragment) != 64    # multibase encoding is longer/different


def test_T11_V9_verification_method_helper_rejects_non_did_key():
    with pytest.raises(ValueError, match="did:key"):
        verification_method("did:web:example.com")
    with pytest.raises(ValueError, match="did:key"):
        verification_method("not-a-did")
    with pytest.raises(ValueError, match="multibase"):
        verification_method("did:key:NoZPrefix")


def test_T11_V9_verify_rejects_tampered_verification_method(dao, agent_did):
    """After signing, if proof.verificationMethod points to a fragment
    that doesn't match the issuer DID, verify must reject."""
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered["proof"] = dict(tampered["proof"])
    # Point to a fragment that's not the issuer's multibase body
    tampered["proof"]["verificationMethod"] = dao.as_did() + "#evil-fragment"
    result = verify_intent_mandate(tampered)
    assert result.ok is False
    assert "verificationMethod mismatch" in result.reason


# =====================================================================
# V-1: signature covers proof options (per VC Data Integrity §4.3)
# =====================================================================
#
# The four proof-option fields - type, created, verificationMethod,
# proofPurpose - must be inside the signed payload. Tampering each
# one after signing must invalidate the signature.


def _expect_sig_invalid(result):
    """The exact reason string depends on which gate fires first
    (proofPurpose check fires before sig verify, for example).
    Accept either 'signature invalid' (sig verify path) or the
    pre-sig structural gates that fire on tampered proof options."""
    assert result.ok is False
    assert (
        "signature invalid" in result.reason
        or "wrong proof purpose" in result.reason
        or "verificationMethod mismatch" in result.reason
    ), f"unexpected reason: {result.reason!r}"


def test_T11_V1_intent_tampering_proof_created_invalidates_signature(
    dao, agent_did,
):
    """Pre-V-1: proof.created could be rewritten after signing without
    detection. Now it's inside the signed payload."""
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered["proof"] = dict(tampered["proof"])
    tampered["proof"]["created"] = "2099-12-31T23:59:59+00:00"
    _expect_sig_invalid(verify_intent_mandate(tampered))


def test_T11_V1_intent_tampering_proof_purpose_invalidates_signature(
    dao, agent_did,
):
    """Pre-V-1: an attacker could change proofPurpose from
    capabilityInvocation to assertionMethod without breaking the
    signature - turning a delegation into an assertion. Now caught
    by the gate (structural) AND by the signature (if the gate is
    bypassed somehow)."""
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered["proof"] = dict(tampered["proof"])
    tampered["proof"]["proofPurpose"] = "assertionMethod"
    _expect_sig_invalid(verify_intent_mandate(tampered))


def test_T11_V1_intent_tampering_proof_type_invalidates(dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered["proof"] = dict(tampered["proof"])
    tampered["proof"]["type"] = "JsonWebSignature2020"
    result = verify_intent_mandate(tampered)
    assert result.ok is False
    # Gate fires first with "unsupported proof type" - that's correct.


def test_T11_V1_cart_tampering_proof_created_invalidates(
    dao, seller, agent_did,
):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    cart["proof"] = dict(cart["proof"])
    cart["proof"]["created"] = "2099-12-31T23:59:59+00:00"
    ok, reason = verify_cart_mandate(cart)
    assert ok is False
    assert "signature invalid" in reason


def test_T11_V1_payment_tampering_proof_created_invalidates(
    dao, seller, agent_did,
):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    payment["proof"] = dict(payment["proof"])
    payment["proof"]["created"] = "2099-12-31T23:59:59+00:00"
    ok, reason = verify_payment_mandate(payment)
    assert ok is False
    assert "signature invalid" in reason


def test_T11_V1_intent_tampering_document_invalidates(dao, agent_did):
    """Document tampering still has to break the signature (the
    pre-existing protection)."""
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered["credentialSubject"] = dict(tampered["credentialSubject"])
    tampered["credentialSubject"]["constraints"] = dict(
        tampered["credentialSubject"]["constraints"]
    )
    tampered["credentialSubject"]["constraints"]["max_amount"] = {
        "value": "999999.00", "currency": "USDC",
    }
    result = verify_intent_mandate(tampered)
    assert result.ok is False
    assert "signature invalid" in result.reason


# =====================================================================
# Cross-mandate consistency: same algorithm produces interoperable
# proofs across all three mandate kinds
# =====================================================================


def test_T11_data_integrity_helper_round_trips_arbitrary_payload(dao):
    """Direct round-trip test on the shared _data_integrity helper.
    Builds a minimal document + proof_options dict, signs, then
    verifies with the matching pubkey."""
    from nth_dao.mandate._data_integrity import decode_issuer_pubkey

    document = {"hello": "world", "n": 42}
    proof_options = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": verification_method(dao.as_did()),
        "proofPurpose": "assertionMethod",
    }
    sig_hex = sign_with_data_integrity(
        identity=dao, document=document, proof_options=proof_options,
    )
    assert len(sig_hex) == 128

    pubkey = decode_issuer_pubkey(dao.as_did())
    proof = {**proof_options, "proofValue": sig_hex}
    ok, reason = verify_with_data_integrity(
        document=document, proof=proof, pubkey_bytes=pubkey,
    )
    assert ok is True, reason


def test_T11_data_integrity_helper_refuses_document_with_existing_proof(dao):
    document = {
        "hello": "world",
        "proof": {"proofValue": "old-signature"},
    }
    proof_options = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": verification_method(dao.as_did()),
        "proofPurpose": "assertionMethod",
    }

    with pytest.raises(ValueError, match="must NOT include proof"):
        sign_with_data_integrity(
            identity=dao,
            document=document,
            proof_options=proof_options,
        )


def test_T11_data_integrity_verify_refuses_document_with_existing_proof(dao):
    from nth_dao.mandate._data_integrity import decode_issuer_pubkey

    document = {"hello": "world"}
    proof_options = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": verification_method(dao.as_did()),
        "proofPurpose": "assertionMethod",
    }
    sig_hex = sign_with_data_integrity(
        identity=dao, document=document, proof_options=proof_options,
    )
    proof = {**proof_options, "proofValue": sig_hex}
    with_proof = {**document, "proof": proof}

    ok, reason = verify_with_data_integrity(
        document=with_proof,
        proof=proof,
        pubkey_bytes=decode_issuer_pubkey(dao.as_did()),
    )
    assert ok is False
    assert "must NOT include proof" in reason


def test_T11_data_integrity_helper_rejects_tampered_options(dao):
    from nth_dao.mandate._data_integrity import decode_issuer_pubkey

    document = {"hello": "world"}
    proof_options = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": verification_method(dao.as_did()),
        "proofPurpose": "assertionMethod",
    }
    sig_hex = sign_with_data_integrity(
        identity=dao, document=document, proof_options=proof_options,
    )
    pubkey = decode_issuer_pubkey(dao.as_did())
    # Tamper an option
    tampered_proof = {
        **proof_options,
        "created": "2099-12-31T23:59:59+00:00",
        "proofValue": sig_hex,
    }
    ok, reason = verify_with_data_integrity(
        document=document, proof=tampered_proof, pubkey_bytes=pubkey,
    )
    assert ok is False
    assert "signature invalid" in reason


def test_T11_data_integrity_helper_rejects_tampered_document(dao):
    from nth_dao.mandate._data_integrity import decode_issuer_pubkey

    document = {"hello": "world"}
    proof_options = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": verification_method(dao.as_did()),
        "proofPurpose": "assertionMethod",
    }
    sig_hex = sign_with_data_integrity(
        identity=dao, document=document, proof_options=proof_options,
    )
    pubkey = decode_issuer_pubkey(dao.as_did())
    proof = {**proof_options, "proofValue": sig_hex}
    # Tamper the document
    tampered_doc = {"hello": "tampered"}
    ok, reason = verify_with_data_integrity(
        document=tampered_doc, proof=proof, pubkey_bytes=pubkey,
    )
    assert ok is False
    assert "signature invalid" in reason


# =====================================================================
# Migration: old-format proofs (raw-hex fragment) must be rejected
# =====================================================================


def test_T11_legacy_raw_hex_verification_method_rejected(dao, agent_did):
    """Synthesize a v0.9.x-style proof where verificationMethod points
    to the raw hex pubkey. Verify must reject due to V-9 alignment."""
    intent = _signed_intent(dao, agent_did)
    # Mutate to legacy raw-hex fragment
    intent["proof"] = dict(intent["proof"])
    intent["proof"]["verificationMethod"] = (
        f"{dao.as_did()}#{dao.pubkey_hex}"
    )
    result = verify_intent_mandate(intent)
    assert result.ok is False
    assert "verificationMethod mismatch" in result.reason


# =====================================================================
# Sanity: well-known did:key vector to nail down the multibase format
# =====================================================================


def test_T11_verification_method_known_vector():
    """Spot-check format using a real Ed25519 did:key string. This
    catches anyone refactoring `verification_method` into something
    that drops the # or rearranges the body."""
    # Real-format did:key for an Ed25519 pubkey (multibase z-string).
    did = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSrugzCmQyDmgT1jBdU"
    vm = verification_method(did)
    assert vm == did + "#z6MkpTHR8VNsBxYAAWHut2Geadd9jSrugzCmQyDmgT1jBdU"
