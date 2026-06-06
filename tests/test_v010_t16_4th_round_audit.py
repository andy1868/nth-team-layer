"""4th-round independent audit findings: F-1 through F-4 fixes.

  F-1  /api/mandates/{kind}/{digest} returns Cache-Control: private
       (not public). Mandate bodies carry counterparty / amount /
       settlement data - shared proxy caches must not retain them.
  F-2  cart_satisfies_intent / payment_satisfies_cart with
       require_signed=True now run real verify_*_mandate, not just
       a "proof block is a dict" shape check. Catches fabricated
       intents with fake proof dicts that the previous theatrical
       gate would have admitted as input to the constraint oracle.
  F-3  emit_settlement_completed now also verifies that
       settlement_choice matches the prior payment.authorised
       event's authorised_choice. Without this, a rogue adapter
       could complete via an unauthorised rail while the audit
       chain still showed a matching authorise+complete pair.
  F-4  RateLimiter no longer leaks memory under unbounded actor_id
       cardinality. Empty buckets are removed via gc_empty_buckets(),
       and the per-key dict is capped at max_tracked_keys (LRU evict).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from nth_dao.event_bus import EventBus
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    cart_satisfies_intent,
    sign_cart_mandate,
)
from nth_dao.mandate.events import (
    emit_payment_authorised,
    emit_settlement_completed,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    payment_mandate_digest,
    payment_satisfies_cart,
    sign_payment_mandate,
)
from nth_dao.web import create_app
from nth_dao.web.rate_limit import RateLimiter


# ----- fixtures -----


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t16-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t16-seller")


@pytest.fixture
def hijacker() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t16-hijacker")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t16-agent").as_did()


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
# F-1: Cache-Control: private on mandate bodies
# =====================================================================


def test_T16_F1_get_mandate_returns_cache_private(
    tmp_path, dao, seller, agent_did,
):
    client = TestClient(create_app(tmp_path))
    intent = _signed_intent(dao, agent_did, seller=seller)
    # actor_id is now explicit-required on /api/mandates/* routes
    client.post("/api/mandates/store", json={
        "kind": "intent", "mandate": intent, "actor_id": "admin",
    })
    digest = intent_mandate_digest(intent)

    resp = client.get(f"/api/mandates/intent/{digest}?actor_id=admin")
    cc = resp.headers.get("Cache-Control", "")
    assert "private" in cc, (
        "Mandate bodies leak counterparty / amount / rail data. "
        "Cache-Control must be 'private', not 'public', to prevent "
        "shared-proxy caching from defeating V-28 auth."
    )
    assert "public" not in cc


# =====================================================================
# F-2: V-21 actually verifies signatures
# =====================================================================


def test_T16_F2_fake_proof_block_no_longer_passes_cart_satisfies(
    dao, seller, agent_did,
):
    """Pre-fix: cart_satisfies_intent accepted any intent with a
    proof block that's a DICT, regardless of whether the dict was
    a real signature. Post-fix: the gate runs real verification."""
    # Fabricate an intent with a fake proof dict
    fake_intent = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did, purpose="fake",
        constraints={
            "max_amount": {"value": "999999.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    # Slap on a junk proof block instead of really signing
    fake_intent["proof"] = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": dao.as_did() + "#fake",
        "proofPurpose": "capabilityInvocation",
        "proofValue": "0" * 128,    # 128 hex chars of zero
    }
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(fake_intent))
    ok, reason = cart_satisfies_intent(cart, fake_intent)
    assert ok is False
    assert "intent must be signed" in reason
    # Confirm the rejection is from REAL verification, not just the
    # shape check (the test would have passed under the old code if
    # this was the shape check).
    assert "signature" in reason.lower() or "verificationmethod" in reason.lower()


def test_T16_F2_fake_proof_block_no_longer_passes_payment_satisfies(
    dao, seller, agent_did,
):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    # Fabricate a payment with a fake proof dict
    fake_payment = build_payment_mandate(
        issuer_did=dao.as_did(), payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc", expires_at=_future(900),
    )
    fake_payment["proof"] = {
        "type": "Ed25519Signature2020",
        "created": "2026-06-05T00:00:00+00:00",
        "verificationMethod": dao.as_did() + "#fake",
        "proofPurpose": "capabilityInvocation",
        "proofValue": "0" * 128,
    }
    ok, reason = payment_satisfies_cart(fake_payment, cart)
    assert ok is False
    assert "payment must be signed" in reason


def test_T16_F2_opt_out_still_works(dao, seller, agent_did):
    """The require_signed=False escape hatch (V-21c) remains intact
    for tooling that has done out-of-band verification."""
    unsigned_intent = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did, purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    unsigned_cart = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(unsigned_intent),
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    )
    ok, _ = cart_satisfies_intent(
        unsigned_cart, unsigned_intent, require_signed=False,
    )
    assert ok is True


# =====================================================================
# F-3: emit_settlement_completed verifies settlement_choice consistency
# =====================================================================


@pytest.fixture
def bus(tmp_path: Path, dao) -> EventBus:
    return EventBus(tmp_path, identity=dao)


def test_T16_F3_settlement_choice_must_match_prior_authorisation(
    bus, dao, seller, agent_did,
):
    """A rogue adapter completes via a rail the DAO did NOT authorise
    while the chain check (V-34) still passes because digest matches.
    F-3 closes this by also comparing settlement_choice."""
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    # DAO authorised x402:usdc
    emit_payment_authorised(bus, payment)
    digest = payment_mandate_digest(payment)

    # Adapter tries to "complete" via a DIFFERENT rail
    with pytest.raises(ValueError, match="does not match the authorised choice"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex=digest,
            adapter="evil",
            settlement_choice="evil_rail:fake_asset",
            outcome="success",
        )


def test_T16_F3_matching_choice_succeeds(bus, dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = _signed_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, seller, cart_mandate_digest(cart))
    emit_payment_authorised(bus, payment)

    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex=payment_mandate_digest(payment),
        adapter="x402",
        settlement_choice="x402:usdc",   # matches
        outcome="success",
    )
    assert ev.event_type == "settlement.completed"


def test_T16_F3_opt_out_skips_choice_check_too(bus):
    """require_prior_authorisation=False bypasses BOTH the prior-
    existence AND the choice-match checks (since both depend on
    finding the prior event). F-6 logs a warning so the bypass
    leaves a forensic trace."""
    ev = emit_settlement_completed(
        bus,
        payment_mandate_digest_hex="a" * 64,
        adapter="legacy",
        settlement_choice="legacy:migration",
        outcome="success",
        require_prior_authorisation=False,
    )
    assert ev.event_type == "settlement.completed"


def test_T16_F6_opt_out_emits_warning_log(bus, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="nth_dao.mandate.events"):
        emit_settlement_completed(
            bus,
            payment_mandate_digest_hex="a" * 64,
            adapter="legacy",
            settlement_choice="legacy:migration",
            outcome="success",
            require_prior_authorisation=False,
        )
    log_msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "WITHOUT prior-authorisation" in m for m in log_msgs
    ), f"expected bypass warning, got: {log_msgs}"


# =====================================================================
# F-4: RateLimiter bounded memory
# =====================================================================


def test_T16_F4_empty_buckets_removed_on_gc():
    """gc_empty_buckets() sweeps keys whose buckets have aged out."""
    lim = RateLimiter(max_per_window=5, window_seconds=0.05)
    for i in range(100):
        lim.check(f"actor-{i}")
    assert len(lim._buckets) == 100
    time.sleep(0.06)
    removed = lim.gc_empty_buckets()
    assert removed == 100
    assert len(lim._buckets) == 0


def test_T16_F4_max_tracked_keys_lru_eviction():
    """When N distinct actors exceed max_tracked_keys, the oldest is
    evicted to make room for the newcomer. Without this cap, an
    attacker enumerating actor_ids could OOM the server."""
    lim = RateLimiter(
        max_per_window=5, window_seconds=10.0, max_tracked_keys=3,
    )
    lim.check("actor-A")
    lim.check("actor-B")
    lim.check("actor-C")
    assert len(lim._buckets) == 3

    lim.check("actor-D")
    # actor-A (oldest) is evicted
    assert "actor-A" not in lim._buckets
    assert "actor-D" in lim._buckets
    assert len(lim._buckets) == 3


def test_T16_F4_max_tracked_keys_validates():
    with pytest.raises(ValueError, match="max_tracked_keys"):
        RateLimiter(
            max_per_window=1, window_seconds=1.0, max_tracked_keys=0,
        )


def test_T16_F4_evicted_actor_restarts_window():
    """An evicted actor's next call starts fresh - no partial-state
    carry-over."""
    lim = RateLimiter(
        max_per_window=2, window_seconds=60.0, max_tracked_keys=2,
    )
    lim.check("actor-A")
    lim.check("actor-A")
    assert not lim.check("actor-A").allowed   # at limit

    # Now exhaust the dict so actor-A gets evicted
    lim.check("actor-B")
    lim.check("actor-C")   # evicts actor-A

    # actor-A's next call - they get a fresh window
    assert lim.check("actor-A").allowed
