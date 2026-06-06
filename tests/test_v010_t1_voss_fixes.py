"""Regression tests for the Voss code-review fixes on T-1 (IntentMandate).

Each test pins one finding from the security review so a future
refactor can't silently regress. V-1 and V-9 ship with the W3C VC
Data Integrity rework (T-1.1); their pin tests live in
test_v010_t11_vc_data_integrity.py to keep that surface together.

Findings covered:

    V-2  max_amount is REQUIRED, no fail-open default
    V-3  allowed_counterparties / allowed_settlement_methods REQUIRED
    V-4  Decimal rejects NaN / Infinity / scientific / whitespace
    V-5  issuer=None does not crash verify
    V-6  VerifyResult.__bool__ returns ok, fixing the truthy-tuple trap
    V-7  verify raises on cannot-determine (PyNaCl missing)
    V-8  intent_expiry_status distinguishes MALFORMED from EXPIRED
    V-10 intent_id keeps full 128 bit UUID
    V-11 INTENT_CONTEXT / INTENT_TYPE are immutable
    V-12 validUntil must be after issuanceDate, with a sane cap
    V-13 digest stable across signing (validates _strip_proof reuse)
    V-15 unknown constraint keys rejected
    V-17 did:key length is bounded
    V-18 re-signing rejected
    V-20 verify failures emit a structured log line
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate import intent as intent_module
from nth_dao.mandate.intent import (
    INTENT_CONTEXT,
    INTENT_PROOF_PURPOSE,
    INTENT_TYPE,
    ExpiryStatus,
    VerifyResult,
    build_intent_mandate,
    intent_expiry_status,
    intent_mandate_digest,
    is_intent_expired,
    sign_intent_mandate,
    verify_intent_mandate,
)


# ===== shared fixtures =====


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="voss-dao")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="voss-agent").as_did()


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _good_constraints() -> Dict[str, Any]:
    return {
        "max_amount": {"value": "100.00", "currency": "USDC"},
        "allowed_counterparties": [],
        "allowed_settlement_methods": ["x402:usdc"],
    }


def _build(dao: AgentIdentity, agent_did: str, **overrides) -> Dict[str, Any]:
    kwargs = dict(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints=_good_constraints(),
        expires_at=_future(86400),
    )
    kwargs.update(overrides)
    return build_intent_mandate(**kwargs)


# =====================================================================
# V-2: max_amount is REQUIRED
# =====================================================================


def test_voss_V2_max_amount_required(dao, agent_did):
    """Pre-fix: omitting max_amount produced a mandate with NO spending
    cap - unlimited authorisation by accident. Now it must raise."""
    constraints = _good_constraints()
    del constraints["max_amount"]
    with pytest.raises(ValueError, match="max_amount is required"):
        _build(dao, agent_did, constraints=constraints)


# =====================================================================
# V-3: allowed_counterparties / methods REQUIRED, [] = fail-closed
# =====================================================================


def test_voss_V3a_allowed_counterparties_required(dao, agent_did):
    constraints = _good_constraints()
    del constraints["allowed_counterparties"]
    with pytest.raises(ValueError, match="allowed_counterparties is required"):
        _build(dao, agent_did, constraints=constraints)


def test_voss_V3b_allowed_settlement_methods_required(dao, agent_did):
    constraints = _good_constraints()
    del constraints["allowed_settlement_methods"]
    with pytest.raises(
        ValueError, match="allowed_settlement_methods is required"
    ):
        _build(dao, agent_did, constraints=constraints)


def test_voss_V3c_empty_lists_round_trip_as_fail_closed(dao, agent_did):
    """Empty lists are LEGAL (caller's explicit fail-closed choice);
    they must round-trip into the credentialSubject so a downstream
    verifier sees the same fail-closed posture, not a 'missing' field
    that could be interpreted as permissive."""
    mandate = _build(dao, agent_did)
    constraints = mandate["credentialSubject"]["constraints"]
    assert constraints["allowed_counterparties"] == []
    assert constraints["allowed_settlement_methods"] == ["x402:usdc"]


# =====================================================================
# V-4: NaN / Infinity / scientific notation / whitespace rejected
# =====================================================================


@pytest.mark.parametrize(
    "bad_value",
    ["NaN", "nan", "Infinity", "-Infinity", "inf"],
    ids=["NaN", "nan-lower", "Infinity", "neg-Infinity", "inf"],
)
def test_voss_V4a_decimal_rejects_nan_and_infinity(dao, agent_did, bad_value):
    """The previous check ``parsed <= 0`` returned False for NaN and
    Infinity (both fail every comparison), letting them through as
    'positive amounts'. Decimal.is_finite() catches them properly."""
    with pytest.raises(ValueError):
        _build(
            dao, agent_did,
            constraints={
                **_good_constraints(),
                "max_amount": {"value": bad_value, "currency": "USDC"},
            },
        )


@pytest.mark.parametrize(
    "bad_value", ["1e10", "1.5E5", "1e-3"], ids=["e10", "E5", "neg-exp"]
)
def test_voss_V4b_decimal_rejects_scientific_notation(
    dao, agent_did, bad_value,
):
    """Scientific notation parses as Decimal but settlement adapters
    may not handle '1e2' identically to '100' - reject at the
    protocol layer."""
    with pytest.raises(ValueError, match="scientific"):
        _build(
            dao, agent_did,
            constraints={
                **_good_constraints(),
                "max_amount": {"value": bad_value, "currency": "USDC"},
            },
        )


@pytest.mark.parametrize(
    "bad_value", [" 100", "100 ", " 100 ", "\t100"],
    ids=["lead-space", "trail-space", "both", "tab"],
)
def test_voss_V4c_decimal_rejects_surrounding_whitespace(
    dao, agent_did, bad_value,
):
    """Decimal('  100  ') is 100, but canonical_json keeps the
    original string in the signed payload - so a downstream verifier
    using strict-string comparison would diverge."""
    with pytest.raises(ValueError, match="whitespace"):
        _build(
            dao, agent_did,
            constraints={
                **_good_constraints(),
                "max_amount": {"value": bad_value, "currency": "USDC"},
            },
        )


# =====================================================================
# V-5: mandate.issuer = None must not crash verify
# =====================================================================


def test_voss_V5_verify_handles_none_issuer(dao, agent_did):
    """Pre-fix: ``mandate.get('issuer', '')`` returns None when the
    key is present-but-None, then ``.startswith`` raises AttributeError,
    which got caught by a broad ``except Exception`` and surfaced as a
    misleading 'signature invalid' message. The hardened code returns
    a structured 'unsupported issuer scheme' result."""
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    signed["issuer"] = None    # the failure mode the original missed
    result = verify_intent_mandate(signed)
    assert result.ok is False
    assert "unsupported issuer scheme" in result.reason


def test_voss_V5b_verify_handles_missing_issuer(dao, agent_did):
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    del signed["issuer"]
    result = verify_intent_mandate(signed)
    assert result.ok is False
    assert "unsupported issuer scheme" in result.reason


# =====================================================================
# V-6: VerifyResult.__bool__ returns ok (not "always truthy")
# =====================================================================


def test_voss_V6a_verify_result_bool_reflects_ok(dao, agent_did):
    """Pre-fix: ``Tuple[bool, str]`` is truthy when non-empty, so
    ``if not verify_intent_mandate(m):`` was ALWAYS False - guards
    using that idiom silently failed open. NamedTuple with __bool__
    override fixes it."""
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    good = verify_intent_mandate(signed)
    assert bool(good) is True
    assert good          # truthy when ok

    tampered = dict(signed)
    tampered["issuer"] = "did:key:zEVIL" + "1" * 44
    bad = verify_intent_mandate(tampered)
    assert bool(bad) is False
    assert not bad       # the legacy idiom now works correctly


def test_voss_V6b_verify_result_preserves_tuple_api(dao, agent_did):
    """Back-compat: ``ok, reason = verify(...)`` must still work, and
    indexing by 0/1 must still work. NamedTuple inherits both from
    tuple."""
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    result = verify_intent_mandate(signed)
    ok, reason = result
    assert ok is True
    assert reason == "ok"
    assert result[0] is True
    assert result[1] == "ok"
    assert isinstance(result, tuple)


def test_voss_V6c_verify_result_is_named_tuple(dao, agent_did):
    """Field access keeps callers from re-indexing if the tuple
    layout ever changes."""
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    result = verify_intent_mandate(signed)
    assert isinstance(result, VerifyResult)
    assert result.ok is True
    assert result.reason == "ok"


# =====================================================================
# V-7: verify raises (not silently returns False) on missing crypto
# =====================================================================


def test_voss_V7_verify_raises_when_crypto_unavailable(
    dao, agent_did, monkeypatch,
):
    """Returning ``(False, ...)`` when we cannot DECIDE conflates
    'cryptographically invalid' with 'I can't tell' - fail-closed in
    the wrong direction, because audit systems treat all (False, *)
    the same way. The hardened code raises so the caller HAS to
    handle it explicitly."""
    monkeypatch.setattr(intent_module, "_CRYPTO_AVAILABLE", False)
    monkeypatch.setattr(intent_module, "_CRYPTO_IMPORT_ERROR", "stub")
    with pytest.raises(RuntimeError, match="verification requires PyNaCl"):
        verify_intent_mandate({"any": "mandate"})


# =====================================================================
# V-8: intent_expiry_status distinguishes MALFORMED from EXPIRED
# =====================================================================


def test_voss_V8a_status_valid_for_fresh(dao, agent_did):
    mandate = _build(dao, agent_did)
    assert intent_expiry_status(mandate) == ExpiryStatus.VALID
    assert is_intent_expired(mandate) is False


def test_voss_V8b_status_expired_for_stale(dao, agent_did):
    mandate = _build(
        dao, agent_did,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        issued_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert intent_expiry_status(mandate) == ExpiryStatus.EXPIRED
    assert is_intent_expired(mandate) is True


@pytest.mark.parametrize(
    "valid_until",
    [None, "", "not-a-date", "2026-13-99T00:00:00+00:00", 12345],
    ids=["none", "empty", "garbage-string", "impossible-date", "non-string"],
)
def test_voss_V8c_status_malformed_for_corrupt_timestamp(valid_until):
    """The previous boolean returned True ('expired') for ANY parse
    failure - so the UI showed 'expired' over a corrupted mandate.
    Now MALFORMED is its own state; the boolean convenience returns
    False for malformed (because it isn't EXPIRED; the caller must
    use the status enum to detect corruption)."""
    mandate = {"validUntil": valid_until}
    assert intent_expiry_status(mandate) == ExpiryStatus.MALFORMED
    assert is_intent_expired(mandate) is False


def test_voss_V8d_status_malformed_for_naive_timestamp():
    """A naive timestamp shouldn't slip past build (we reject at build
    time) but verify-side must defend against tampering."""
    mandate = {"validUntil": "2026-06-01T00:00:00"}   # no tz
    assert intent_expiry_status(mandate) == ExpiryStatus.MALFORMED


# =====================================================================
# V-10: intent_id keeps full UUID4 entropy (32 hex chars)
# =====================================================================


def test_voss_V10_intent_id_full_uuid_length(dao, agent_did):
    """Auto-generated intent_id is 32 hex chars (full UUID4); the
    explicit override path accepts whatever the caller passes (so
    short test IDs still work)."""
    mandate = _build(dao, agent_did)
    assert len(mandate["credentialSubject"]["intent_id"]) == 32

    explicit = _build(dao, agent_did, intent_id="custom-test-id")
    assert explicit["credentialSubject"]["intent_id"] == "custom-test-id"


# =====================================================================
# V-11: INTENT_CONTEXT / INTENT_TYPE are immutable
# =====================================================================


def test_voss_V11a_context_is_tuple():
    assert isinstance(INTENT_CONTEXT, tuple)
    assert isinstance(INTENT_TYPE, tuple)
    # And cannot be mutated even by accident
    with pytest.raises((AttributeError, TypeError)):
        INTENT_CONTEXT.append("https://evil")   # type: ignore[attr-defined]


def test_voss_V11b_mandate_carries_fresh_lists(dao, agent_did):
    """Build returns a list (for JSON serialisation), and mutating
    that list must not affect future mandates."""
    one = _build(dao, agent_did)
    one["@context"].append("https://evil")
    two = _build(dao, agent_did)
    assert "https://evil" not in two["@context"]


# =====================================================================
# V-12: validUntil > issuanceDate, and bounded by _MAX_VALIDITY
# =====================================================================


def test_voss_V12a_validUntil_must_be_after_issuance(dao, agent_did):
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="must be strictly after"):
        _build(
            dao, agent_did,
            issued_at=issued,
            expires_at=(issued - timedelta(seconds=1)).isoformat(),
        )


def test_voss_V12b_equal_validUntil_and_issuance_rejected(dao, agent_did):
    """validUntil == issuanceDate is a zero-second window - reject."""
    issued = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="must be strictly after"):
        _build(
            dao, agent_did,
            issued_at=issued,
            expires_at=issued.isoformat(),
        )


def test_voss_V12c_excessive_validity_rejected(dao, agent_did):
    """A 100-year mandate is almost certainly a configuration bug."""
    issued = datetime.now(timezone.utc)
    far_future = (issued + timedelta(days=365 * 100)).isoformat()
    with pytest.raises(ValueError, match="exceeds"):
        _build(dao, agent_did, issued_at=issued, expires_at=far_future)


def test_voss_V12d_one_year_at_the_boundary_accepted(dao, agent_did):
    """The protocol cap is 365 days, exactly at the boundary works."""
    issued = datetime.now(timezone.utc)
    one_year = (issued + timedelta(days=365)).isoformat()
    mandate = _build(dao, agent_did, issued_at=issued, expires_at=one_year)
    assert mandate["validUntil"] == one_year


# =====================================================================
# V-13: digest is stable across signing
# =====================================================================


def test_voss_V13_digest_stable_across_signing(dao, agent_did):
    """``intent_mandate_digest`` excludes the proof block so the value
    a Cart binds to does not move when the issuer signs. This test
    pins that invariant: Cart can compute the digest BEFORE the
    Intent is signed and bind to it; the post-sign digest equals the
    pre-sign digest."""
    unsigned = _build(dao, agent_did)
    pre_digest = intent_mandate_digest(unsigned)
    signed = sign_intent_mandate(unsigned, dao)
    post_digest = intent_mandate_digest(signed)
    assert pre_digest == post_digest
    # Sanity: also that the unsigned input was not mutated by sign
    assert "proof" not in unsigned


# =====================================================================
# V-15: unknown constraint keys rejected
# =====================================================================


def test_voss_V15_unknown_constraint_key_rejected(dao, agent_did):
    """Typos like ``allowed_counterparty`` (singular) used to be
    silently ignored, which then defaulted to the fail-open empty
    list. Now they raise loudly so the typo gets fixed."""
    constraints = _good_constraints()
    constraints["allowed_counterparty"] = ["did:key:z6Mkxxx"]   # typo!
    with pytest.raises(ValueError, match="unknown constraint keys"):
        _build(dao, agent_did, constraints=constraints)


# =====================================================================
# V-17: did:key length is bounded
# =====================================================================


def test_voss_V17a_oversized_issuer_did_rejected(agent_did):
    """A 100 KB did:key string passing the old unbounded regex was a
    DoS vector against canonical_json + signature verification."""
    huge_did = "did:key:z" + "1" * 10000
    with pytest.raises(ValueError, match="issuer_did"):
        build_intent_mandate(
            issuer_did=huge_did,
            agent_did=agent_did,
            purpose="x",
            constraints=_good_constraints(),
            expires_at=_future(3600),
        )


def test_voss_V17b_too_short_did_rejected(dao, agent_did):
    """did:key:z1 is shape-valid for the OLD regex but doesn't encode
    an Ed25519 pubkey of any usable length."""
    with pytest.raises(ValueError, match="issuer_did"):
        build_intent_mandate(
            issuer_did="did:key:z123",
            agent_did=agent_did,
            purpose="x",
            constraints=_good_constraints(),
            expires_at=_future(3600),
        )


# =====================================================================
# V-18: re-signing is rejected
# =====================================================================


def test_voss_V18_resigning_rejected(dao, agent_did):
    """``sign_intent_mandate`` used to silently overwrite an existing
    proof, which is almost always a bug (whichever signature was on
    the input vanishes). Refuse loudly; explicit re-sign flows should
    rebuild from source."""
    signed_once = sign_intent_mandate(_build(dao, agent_did), dao)
    with pytest.raises(ValueError, match="already carries a proof"):
        sign_intent_mandate(signed_once, dao)


# =====================================================================
# V-20: verify failures emit a structured log line
# =====================================================================


def test_voss_V20_verify_failure_is_logged(dao, agent_did, caplog):
    """Every verify failure path goes through ``_verify_fail`` which
    logs at INFO with the issuer + intent_id for forensic correlation.
    Pin one failure case so the log path can't silently regress."""
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    # Tamper a signed field to invalidate the signature
    signed["credentialSubject"] = dict(signed["credentialSubject"])
    signed["credentialSubject"]["purpose"] = "tampered"

    with caplog.at_level(logging.INFO, logger="nth_dao.mandate.intent"):
        result = verify_intent_mandate(signed)

    assert result.ok is False
    # The log line names the failure and includes the issuer DID for
    # forensic correlation with other events.
    log_messages = [r.getMessage() for r in caplog.records]
    assert any("verify failed" in m and dao.as_did() in m for m in log_messages), (
        f"expected verify-failed log line referencing issuer, got: {log_messages}"
    )


# =====================================================================
# Spot-check: the proof block on a fresh sign carries the expected
# proofPurpose constant. This catches future refactors that try to
# rename the constant and forget to update the verify-side check.
# =====================================================================


def test_voss_intent_proof_purpose_constant_alignment(dao, agent_did):
    signed = sign_intent_mandate(_build(dao, agent_did), dao)
    assert signed["proof"]["proofPurpose"] == INTENT_PROOF_PURPOSE
    assert INTENT_PROOF_PURPOSE == "capabilityInvocation"
