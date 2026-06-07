"""Architect audit M-2 (2026-06-07): explicit wildcard rejection in
build_intent_mandate.

Earlier docstring revisions promised a ``["*"]`` wildcard for
``allowed_counterparties`` / ``allowed_settlement_methods``. The current
docstring says "wildcards intentionally unsupported", but the run-time
rejection was only a side effect of ``_is_did_key("*")`` returning False
- producing a confusing "must be did:key, got '*'" error.

This pins the explicit wildcard guard with a clear actionable error
message, so:
  1. wildcard semantics can never be re-added by accident
  2. the run-time error matches the documentation
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nth_dao.mandate.intent import build_intent_mandate


def _near_future_iso() -> str:
    """30-day expiry - well under the 365-day protocol cap."""
    return (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()


def _valid_constraints(**overrides):
    base = {
        "max_amount": {"value": "10.00", "currency": "USD"},
        "allowed_counterparties": [
            "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH",
        ],
        "allowed_settlement_methods": ["x402:usdc"],
    }
    base.update(overrides)
    return base


def _build(constraints):
    return build_intent_mandate(
        issuer_did="did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH",
        agent_did="did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH",
        purpose="test",
        constraints=constraints,
        expires_at=_near_future_iso(),
    )


# ===== counterparties wildcard =====


def test_M2_counterparties_wildcard_star_rejected_with_clear_message():
    """`["*"]` must produce an actionable error mentioning wildcards,
    not a generic "must be did:key" message."""
    with pytest.raises(ValueError, match="wildcard.*not supported"):
        _build(_valid_constraints(allowed_counterparties=["*"]))


def test_M2_counterparties_wildcard_among_valid_dids_still_rejected():
    """Mixed allow-list with one `"*"` entry must still be rejected -
    can't sneak the wildcard past by burying it in real DIDs."""
    with pytest.raises(ValueError, match="wildcard.*not supported"):
        _build(_valid_constraints(allowed_counterparties=[
            "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH",
            "*",
        ]))


# ===== settlement methods wildcard =====


def test_M2_settlement_methods_wildcard_star_rejected_with_clear_message():
    """Same defence applied to settlement methods."""
    with pytest.raises(ValueError, match="wildcard.*not supported"):
        _build(_valid_constraints(allowed_settlement_methods=["*"]))


def test_M2_settlement_methods_wildcard_among_valid_still_rejected():
    with pytest.raises(ValueError, match="wildcard.*not supported"):
        _build(_valid_constraints(allowed_settlement_methods=[
            "x402:usdc",
            "*",
        ]))


# ===== explicit allow-list still works =====


def test_M2_explicit_did_allowlist_accepted():
    """No regression: a normal explicit allow-list still builds."""
    result = _build(_valid_constraints())
    assert (
        result["credentialSubject"]["constraints"]["allowed_counterparties"]
        == ["did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"]
    )


def test_M2_empty_lists_still_accepted_as_fail_closed():
    """Empty list is the documented "fail-closed" sentinel - must stay
    legal even after the wildcard guard."""
    result = _build(_valid_constraints(
        allowed_counterparties=[],
        allowed_settlement_methods=[],
    ))
    cs = result["credentialSubject"]["constraints"]
    assert cs["allowed_counterparties"] == []
    assert cs["allowed_settlement_methods"] == []
