"""Tests for nth_dao.mandate.intent (v0.10 Sprint Zero T-1).

IntentMandate is the AUTHORISATION half of the AP2-shape Mandate
triad: an issuer (DAO or human) signs a delegation that authorises
a specific agent to act within bounded parameters.

12 tests covering W3C VC shape, sign+verify round trip, tamper /
proofPurpose / issuer mismatch rejections, digest stability across
signing, expiry helper, constraint validation, facade re-export.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.intent import (
    INTENT_CONTEXT,
    INTENT_TYPE,
    PROOF_PURPOSE,
    PROOF_TYPE,
    build_intent_mandate,
    intent_mandate_digest,
    is_intent_expired,
    sign_intent_mandate,
    verify_intent_mandate,
)


@pytest.fixture
def issuer() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="dao-treasury")


@pytest.fixture
def other_issuer() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="rogue")


@pytest.fixture
def agent_did() -> str:
    """A plausible did:key for the agent being authorised. We don't
    need the matching identity - the agent is the SUBJECT, not the
    signer."""
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob").as_did()


def _make_mandate(issuer: AgentIdentity, agent_did: str, **overrides):
    """Helper - build a fresh unsigned IntentMandate."""
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    defaults = dict(
        issuer_did=issuer.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=expires,
    )
    defaults.update(overrides)
    return build_intent_mandate(**defaults)


# ===== 1. W3C VC shape =====


def test_T1_01_build_has_w3c_vc_shape(issuer, agent_did):
    """The dict produced by build must carry the standard W3C VC
    keys with correct values - so any external VC verifier (AP2
    facilitator, Universal Resolver) can consume it."""
    mandate = _make_mandate(issuer, agent_did)
    assert mandate["@context"] == INTENT_CONTEXT
    assert mandate["@context"][0].startswith("https://www.w3.org/")
    assert mandate["type"] == INTENT_TYPE
    assert mandate["type"][0] == "VerifiableCredential"
    assert mandate["issuer"] == issuer.as_did()
    assert "issuanceDate" in mandate
    assert "validFrom" in mandate
    assert "validUntil" in mandate
    assert "credentialSubject" in mandate
    assert "proof" not in mandate   # unsigned by default


# ===== 2. credentialSubject content =====


def test_T1_02_credentialSubject_carries_agent_and_constraints(issuer, agent_did):
    """The agent being authorised goes in credentialSubject.id per W3C
    convention; intent_id is auto-generated; constraints round-trip."""
    mandate = _make_mandate(issuer, agent_did)
    subject = mandate["credentialSubject"]
    assert subject["id"] == agent_did
    assert subject["purpose"] == "buy code review"
    assert len(subject["intent_id"]) == 16
    assert subject["constraints"]["max_amount"] == {
        "value": "100.00", "currency": "USDC",
    }
    assert subject["constraints"]["allowed_settlement_methods"] == ["x402:usdc"]


# ===== 3. sign attaches proof block =====


def test_T1_03_sign_attaches_ed25519_proof(issuer, agent_did):
    """After signing, the mandate carries a proof block with the
    standard Ed25519Signature2020 fields. Original input is not
    mutated."""
    mandate = _make_mandate(issuer, agent_did)
    signed = sign_intent_mandate(mandate, issuer)
    assert "proof" not in mandate   # input untouched
    proof = signed["proof"]
    assert proof["type"] == PROOF_TYPE
    assert proof["proofPurpose"] == PROOF_PURPOSE
    assert proof["verificationMethod"].startswith(issuer.as_did() + "#")
    assert len(proof["proofValue"]) == 128


# ===== 4. signed mandate verifies =====


def test_T1_04_verify_signed_mandate_passes(issuer, agent_did):
    mandate = _make_mandate(issuer, agent_did)
    signed = sign_intent_mandate(mandate, issuer)
    ok, reason = verify_intent_mandate(signed)
    assert ok, reason


# ===== 5. tampered subject fails verification =====


def test_T1_05_verify_rejects_tampered_subject(issuer, agent_did):
    """Mutating ANYTHING in the signed dict (except the proof block
    itself) invalidates the signature."""
    mandate = _make_mandate(issuer, agent_did)
    signed = sign_intent_mandate(mandate, issuer)
    signed["credentialSubject"]["constraints"]["max_amount"]["value"] = "1000000.00"
    ok, reason = verify_intent_mandate(signed)
    assert not ok
    assert "signature invalid" in reason


# ===== 6. unsigned mandate fails verification =====


def test_T1_06_verify_rejects_missing_proof(issuer, agent_did):
    mandate = _make_mandate(issuer, agent_did)
    ok, reason = verify_intent_mandate(mandate)
    assert not ok
    assert "missing proof" in reason


# ===== 7. wrong proofPurpose rejected =====


def test_T1_07_verify_rejects_wrong_proof_purpose(issuer, agent_did):
    """proofPurpose must be capabilityInvocation for an IntentMandate.
    A credential signed with assertionMethod would technically verify
    cryptographically but semantically be the wrong KIND of statement -
    catch it at the verify gate so callers never act on the wrong
    semantic class."""
    mandate = _make_mandate(issuer, agent_did)
    signed = sign_intent_mandate(mandate, issuer)
    signed["proof"]["proofPurpose"] = "assertionMethod"
    ok, reason = verify_intent_mandate(signed)
    assert not ok
    assert "wrong proof purpose" in reason


# ===== 8. issuer DID must match signer =====


def test_T1_08_sign_rejects_issuer_signer_mismatch(issuer, other_issuer, agent_did):
    """The mandate.issuer must match the signer's DID. Signing under
    a different identity would produce a credential no verifier
    accepts (the signature verifies against signer's pubkey but
    issuer.did points elsewhere). Reject at build time."""
    mandate = _make_mandate(issuer, agent_did)   # issuer field = issuer's DID
    with pytest.raises(ValueError, match="issuer DID mismatch"):
        sign_intent_mandate(mandate, other_issuer)   # signed by someone else


# ===== 9. digest stable across signing =====


def test_T1_09_digest_stable_after_signing(issuer, agent_did):
    """Cart and Payment mandates bind to the IntentMandate's digest.
    Signing the intent must not change the digest (which is computed
    over the canonical-JSON of the mandate minus proof) - otherwise
    the binding breaks every time the intent gets re-signed."""
    mandate = _make_mandate(issuer, agent_did)
    d_unsigned = intent_mandate_digest(mandate)
    signed = sign_intent_mandate(mandate, issuer)
    d_signed = intent_mandate_digest(signed)
    assert d_unsigned == d_signed
    # And mutating the subject DOES change the digest
    mutated = sign_intent_mandate(_make_mandate(
        issuer, agent_did, purpose="different",
    ), issuer)
    d_mutated = intent_mandate_digest(mutated)
    assert d_mutated != d_signed


# ===== 10. expiry helper recognises future vs past =====


def test_T1_10_is_expired_window(issuer, agent_did):
    """is_intent_expired is a separate concern from
    verify_intent_mandate - an expired mandate is still
    cryptographically valid, just no longer ACTIONABLE."""
    # Future-dated
    fresh = _make_mandate(
        issuer, agent_did,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    assert is_intent_expired(fresh) is False
    # Past-dated
    stale = _make_mandate(
        issuer, agent_did,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    assert is_intent_expired(stale) is True
    # Custom 'now' parameter - simulate the future, fresh mandate
    # appears expired then
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    assert is_intent_expired(fresh, now=far_future) is True


# ===== 11. constraint validation rejects bad input =====


def test_T1_11_invalid_constraints_rejected(issuer, agent_did):
    """Structural validation surfaces bugs at build time, NOT at the
    counterparty's verifier."""
    # Negative max_amount
    with pytest.raises(ValueError, match="positive"):
        _make_mandate(issuer, agent_did, constraints={
            "max_amount": {"value": "-1.00", "currency": "USDC"},
        })
    # Non-decimal value
    with pytest.raises(ValueError, match="decimal"):
        _make_mandate(issuer, agent_did, constraints={
            "max_amount": {"value": "free", "currency": "USDC"},
        })
    # Lowercase currency
    with pytest.raises(ValueError, match="uppercase"):
        _make_mandate(issuer, agent_did, constraints={
            "max_amount": {"value": "1.00", "currency": "usdc"},
        })
    # Bad counterparty DID
    with pytest.raises(ValueError, match="did:key"):
        _make_mandate(issuer, agent_did, constraints={
            "allowed_counterparties": ["not-a-did"],
        })
    # Bad settlement method (no colon)
    with pytest.raises(ValueError, match="adapter"):
        _make_mandate(issuer, agent_did, constraints={
            "allowed_settlement_methods": ["x402usdc"],
        })
    # Naive timestamp (no tz)
    with pytest.raises(ValueError, match="timezone"):
        _make_mandate(
            issuer, agent_did,
            expires_at=datetime.now().isoformat(),   # no tzinfo
        )
    # Bad agent_did
    with pytest.raises(ValueError, match="agent_did"):
        _make_mandate(issuer, agent_did="not-a-did")
    # Bad issuer_did
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="issuer_did"):
        build_intent_mandate(
            issuer_did="not-a-did", agent_did=agent_did,
            purpose="x", constraints={}, expires_at=expires,
        )


# ===== 12. facade re-export + intent_id stability =====


def test_T1_12_facade_reexport_and_intent_id_stable(issuer, agent_did):
    """The top-level nth_dao namespace must expose the build/sign/verify
    helpers - integrators using `import nth_dao as nth` get them
    without drilling into the mandate package. And a caller-provided
    intent_id round-trips through build."""
    import nth_dao
    assert nth_dao.build_intent_mandate is build_intent_mandate
    assert nth_dao.sign_intent_mandate is sign_intent_mandate
    assert nth_dao.verify_intent_mandate is verify_intent_mandate
    assert nth_dao.intent_mandate_digest is intent_mandate_digest
    assert nth_dao.is_intent_expired is is_intent_expired

    custom = _make_mandate(issuer, agent_did, intent_id="abc123def456")
    assert custom["credentialSubject"]["intent_id"] == "abc123def456"
    signed = sign_intent_mandate(custom, issuer)
    ok, reason = verify_intent_mandate(signed)
    assert ok, reason
