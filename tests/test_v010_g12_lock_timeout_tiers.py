"""G-12 (Voss audit): safe_append_jsonl exposes tiered timeout defaults.

The original safe_append_jsonl hard-coded ``lock_timeout=5.0``. That
forces every caller to either accept the default (no documented
intent) or pick a magic number (no shared meaning across callsites).

G-12 introduces three tier constants:

    LOCK_TIMEOUT_FAST     = 1.0    # hot loops, false-positive ok
    LOCK_TIMEOUT_DEFAULT  = 5.0    # general use, unchanged
    LOCK_TIMEOUT_PATIENT  = 30.0   # audit-critical, must not drop

And migrates the audit-critical reputation paths (endorsement /
revocation) to PATIENT, so transient contention never silently
loses a reputation signal.

Pinned invariants:
  * Three named constants exist on the public ``nth_dao.util`` surface
  * FAST < DEFAULT < PATIENT (ordering matters for "tier" semantics)
  * Default kwarg of safe_append_jsonl equals LOCK_TIMEOUT_DEFAULT
  * WebOfTrustStore endorsement / revocation paths use PATIENT
  * Custom lock_timeout values still propagate end-to-end
"""

from __future__ import annotations

import inspect
import pytest

from nth_dao.util import (
    LOCK_TIMEOUT_DEFAULT,
    LOCK_TIMEOUT_FAST,
    LOCK_TIMEOUT_PATIENT,
    safe_append_jsonl,
)


# ===== constants exist + are floats + are ordered =====


def test_G12_three_tier_constants_are_exported_on_util_surface():
    """The constants must be importable from nth_dao.util - not just
    from nth_dao.util.jsonl_safe - so callsites depend on the stable
    public facade."""
    assert isinstance(LOCK_TIMEOUT_FAST, float)
    assert isinstance(LOCK_TIMEOUT_DEFAULT, float)
    assert isinstance(LOCK_TIMEOUT_PATIENT, float)


def test_G12_tier_ordering_is_strict():
    """Tier values must be strictly increasing - otherwise the
    'tier' abstraction is meaningless."""
    assert LOCK_TIMEOUT_FAST < LOCK_TIMEOUT_DEFAULT < LOCK_TIMEOUT_PATIENT


def test_G12_tier_values_match_documented_intent():
    """The docstring documents specific values; deployments tune via
    these names. Don't drift the numbers without updating downstream
    callsites that rely on them."""
    assert LOCK_TIMEOUT_FAST == 1.0
    assert LOCK_TIMEOUT_DEFAULT == 5.0
    assert LOCK_TIMEOUT_PATIENT == 30.0


# ===== safe_append_jsonl default matches DEFAULT tier =====


def test_G12_safe_append_jsonl_default_kwarg_equals_default_tier():
    """The function default must reference LOCK_TIMEOUT_DEFAULT
    (not a separately-hardcoded 5.0). Otherwise tuning DEFAULT
    centrally wouldn't reach the function default."""
    sig = inspect.signature(safe_append_jsonl)
    default = sig.parameters["lock_timeout"].default
    assert default == LOCK_TIMEOUT_DEFAULT


# ===== custom timeout propagates =====


def test_G12_custom_lock_timeout_still_works(tmp_path):
    """A caller passing a custom timeout (any of the three tiers)
    must succeed end-to-end. Regression guard for the constant-import
    refactor not breaking the existing kwarg behaviour."""
    target = tmp_path / "events.jsonl"
    for tier in (LOCK_TIMEOUT_FAST, LOCK_TIMEOUT_DEFAULT, LOCK_TIMEOUT_PATIENT):
        safe_append_jsonl(
            target, {"tier": tier, "msg": "tier check"},
            lock_timeout=tier,
        )
    # One line per tier
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


# ===== reputation paths use PATIENT tier =====


def test_G12_endorsement_append_uses_patient_tier():
    """The reputation endorsement append must use LOCK_TIMEOUT_PATIENT.
    Reading the source is a heuristic but it's the strongest pinning
    we can do without instrumenting the lock object itself - and the
    G-7 backends used the same source-inspection pattern."""
    from nth_dao.web_of_trust import TrustGraph
    src = inspect.getsource(TrustGraph._append)
    assert "LOCK_TIMEOUT_PATIENT" in src, (
        "endorsement append must declare the PATIENT tier explicitly so "
        "transient contention never silently loses a reputation signal"
    )


def test_G12_revocation_append_uses_patient_tier():
    """Revocations share endorsement's audit-criticality."""
    from nth_dao.web_of_trust import TrustGraph
    src = inspect.getsource(TrustGraph.import_revocation)
    assert "LOCK_TIMEOUT_PATIENT" in src, (
        "revocation append must declare the PATIENT tier; revocations "
        "share endorsement's audit-criticality"
    )


# ===== end-to-end: reputation flow still works =====


def test_G12_endorsement_end_to_end_still_works(tmp_path):
    """After upgrading the tier we should NOT have broken the actual
    endorsement creation flow. Smoke check."""
    from nth_dao.identity import AgentIdentity, crypto_available
    from nth_dao.web_of_trust import TrustGraph, issue_endorsement

    if not crypto_available():
        pytest.skip("PyNaCl not installed - endorsement flow requires crypto")

    alice = AgentIdentity.generate(label="alice")
    bob = AgentIdentity.generate(label="bob")
    tg = TrustGraph(tmp_path)
    e = issue_endorsement(alice, bob.pubkey_hex, "bob")
    assert tg.import_endorsement(e) is True
    # And it landed on disk via safe_append_jsonl (PATIENT-tier path)
    assert tg._endorsements_path.exists()
    lines = tg._endorsements_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
