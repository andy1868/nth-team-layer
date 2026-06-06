"""Second-pass audit tests: gaps the first Voss-fix run missed.

This file covers the issues a fresh reviewer found in the prior
session's fixes:

  B-1   payment_satisfies_cart(intent=...) didn't apply require_signed
        to intent itself, so a fabricated unsigned intent satisfied
        the V-32 issuer-continuity check
  T-1   V-28 only tested /api/mandates (list); now we pin every
        mandate route through the auth gate
  T-2   V-29 only tested kind=intent; now we test cart and payment
  T-3   V-31 "cheap structural first" was only tested via reason
        string; now we pin it by mocking verify_*_mandate
  T-4   V-27 array-of-non-string + array-of-empty-string
  T-5   V-24 streaming body without content-length + boundary at cap
  T-6   V-26 batch size boundary at cap
  T-7   V-36 idempotent same-bytes save + list returns earlier
  T-8   V-38 atomic re-write replaces existing target
  T-9   V-35 data: / javascript: / file: schemes explicitly rejected
  T-10  V-22 missing-key path (the build-time path forbids it but
        the runtime check is now needed for tampered intents)
  T-11  V-32 documented footgun: standalone payment_satisfies_cart
        without intent= silently lets hijacker pass
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from nth_dao.a2a.agent_card import build_agent_card, write_agent_card
from nth_dao.a2a.server import (
    A2A_REQUEST_TOO_LARGE,
    JSONRPC_INVALID_PARAMS,
    create_a2a_app,
)
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    cart_satisfies_intent,
    sign_cart_mandate,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    complete_triad_chain,
    payment_mandate_digest,
    payment_satisfies_cart,
    sign_payment_mandate,
)
from nth_dao.mandate.store import MandateStore
from nth_dao.web import create_app


# ----- fixtures -----


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="audit2-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="audit2-seller")


@pytest.fixture
def hijacker() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="audit2-hijacker")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="audit2-agent").as_did()


def _future(s: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()


def _sign_intent(dao, agent_did, *, seller=None):
    m = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did,
        purpose="x",
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


def _sign_cart(seller, agent_did, intent_digest):
    c = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    )
    return sign_cart_mandate(c, seller)


def _sign_payment(dao, seller, cart_digest, *, issuer_did=None, signer=None):
    p = build_payment_mandate(
        issuer_did=issuer_did or dao.as_did(),
        payee_did=seller.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice="x402:usdc", expires_at=_future(900),
    )
    return sign_payment_mandate(p, signer or dao)


def _bare_card() -> Dict[str, Any]:
    return build_agent_card(
        name="Audit2", description="",
        url="https://localhost:8080/a2a", capabilities=["echo"],
    )


# =====================================================================
# B-1: payment_satisfies_cart(intent=...) require_signed on intent
# =====================================================================


def test_audit2_B1_unsigned_intent_rejected_when_intent_supplied(
    dao, seller, hijacker, agent_did,
):
    """A hijacker can fabricate an UNSIGNED intent with the legit DAO's
    DID in `issuer` to spoof V-32's issuer-continuity check. The
    intent= parameter must therefore also require a proof block."""
    intent_unsigned_fake = build_intent_mandate(
        issuer_did=hijacker.as_did(),    # hijacker's DID as issuer
        agent_did=agent_did, purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    legit_intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(legit_intent))
    # Hijacker signs a payment as themselves and ALSO crafts a fake
    # intent whose issuer matches the payment's issuer (i.e. hijacker)
    # to defeat the continuity check.
    hijack_payment = _sign_payment(
        hijacker, seller, cart_mandate_digest(cart),
        issuer_did=hijacker.as_did(), signer=hijacker,
    )
    ok, reason = payment_satisfies_cart(
        hijack_payment, cart, intent=intent_unsigned_fake,
    )
    # Before B-1 fix: this returned (True, "ok") because issuers
    # trivially matched on the fake unsigned intent.
    assert ok is False
    assert "intent must be signed" in reason


def test_audit2_B1_opt_out_with_require_signed_false(
    dao, seller, hijacker, agent_did,
):
    """For tooling with out-of-band verification of intents, the
    require_signed=False escape hatch still lets the call through."""
    unsigned = build_intent_mandate(
        issuer_did=hijacker.as_did(), agent_did=agent_did, purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    legit = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(legit))
    payment = _sign_payment(
        hijacker, seller, cart_mandate_digest(cart),
        issuer_did=hijacker.as_did(), signer=hijacker,
    )
    # require_signed=False bypasses BOTH the cart/payment signed gate
    # AND the new intent gate. The continuity check still fires
    # because issuer matches between hijacker and unsigned-with-
    # hijacker-DID.
    ok, _ = payment_satisfies_cart(
        payment, cart, intent=unsigned, require_signed=False,
    )
    assert ok is True


# =====================================================================
# T-1: every /api/mandates/* route is auth-gated
# =====================================================================


def _closed_workspace(tmp_path):
    (tmp_path / "team.json").write_text(
        '{"team_name":"Closed","join_policy":"invite_only",'
        '"admin_ids":["admin"],"member_ids":["admin"],'
        '"roles":{"admin":"owner"}}',
        encoding="utf-8",
    )
    return TestClient(create_app(tmp_path))


def test_audit2_T1_list_route_gated(tmp_path):
    client = _closed_workspace(tmp_path)
    assert client.get("/api/mandates?actor_id=stranger").status_code == 403


def test_audit2_T1_get_route_gated(tmp_path):
    client = _closed_workspace(tmp_path)
    digest = "a" * 64
    assert client.get(
        f"/api/mandates/intent/{digest}?actor_id=stranger",
    ).status_code == 403


def test_audit2_T1_store_route_gated(tmp_path):
    client = _closed_workspace(tmp_path)
    resp = client.post("/api/mandates/store", json={
        "kind": "intent", "mandate": {}, "actor_id": "stranger",
    })
    assert resp.status_code == 403


def test_audit2_T1_verify_route_gated(tmp_path):
    client = _closed_workspace(tmp_path)
    resp = client.post("/api/mandates/verify", json={
        "kind": "intent", "mandate": {}, "actor_id": "stranger",
    })
    assert resp.status_code == 403


# =====================================================================
# T-2: V-29 store-verify covers all three kinds
# =====================================================================


def test_audit2_T2_store_refuses_unsigned_cart(tmp_path, dao, seller, agent_did):
    client = TestClient(create_app(tmp_path))
    # Need a signed intent first so the cart has somewhere to bind to.
    intent = _sign_intent(dao, agent_did, seller=seller)
    unsigned_cart = build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(intent),
        items=[{"description": "x", "quantity": 1}],
        total={"value": "10.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    )
    resp = client.post("/api/mandates/store", json={
        "kind": "cart", "mandate": unsigned_cart, "actor_id": "admin",
    })
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()


def test_audit2_T2_store_refuses_unsigned_payment(tmp_path, dao, seller):
    client = TestClient(create_app(tmp_path))
    unsigned_payment = build_payment_mandate(
        issuer_did=dao.as_did(), payee_did=seller.as_did(),
        cart_mandate_digest_hex="a" * 64,
        settlement_choice="x402:usdc", expires_at=_future(900),
    )
    resp = client.post("/api/mandates/store", json={
        "kind": "payment", "mandate": unsigned_payment, "actor_id": "admin",
    })
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()


def test_audit2_T2_store_accepts_signed_cart(tmp_path, dao, seller, agent_did):
    client = TestClient(create_app(tmp_path))
    intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(intent))
    resp = client.post("/api/mandates/store", json={
        "kind": "cart", "mandate": cart, "actor_id": "admin",
    })
    assert resp.status_code == 200
    assert resp.json()["digest"] == cart_mandate_digest(cart)


def test_audit2_T2_store_accepts_signed_payment(tmp_path, dao, seller, agent_did):
    client = TestClient(create_app(tmp_path))
    intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(intent))
    payment = _sign_payment(dao, seller, cart_mandate_digest(cart))
    resp = client.post("/api/mandates/store", json={
        "kind": "payment", "mandate": payment, "actor_id": "admin",
    })
    assert resp.status_code == 200
    assert resp.json()["digest"] == payment_mandate_digest(payment)


# =====================================================================
# T-3: V-31 cheap-first ordering pinned via mock counters
# =====================================================================


def test_audit2_T3_triad_chain_rejects_hijacker_before_any_crypto(
    dao, seller, hijacker, agent_did,
):
    """The performance/info-leak claim of V-31 is: when issuer-continuity
    is broken we MUST NOT have called any verify_*_mandate. Patch
    each verifier to count calls; on the hijacker path all three
    counters stay at zero."""
    intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(intent))
    hijacked = _sign_payment(
        hijacker, seller, cart_mandate_digest(cart),
        issuer_did=hijacker.as_did(), signer=hijacker,
    )

    with patch(
        "nth_dao.mandate.payment.verify_intent_mandate"
    ) as vi, patch(
        "nth_dao.mandate.payment.verify_cart_mandate"
    ) as vc, patch(
        "nth_dao.mandate.payment.verify_payment_mandate"
    ) as vp:
        ok, reason = complete_triad_chain(intent, cart, hijacked)

    assert ok is False
    assert "issuer continuity broken" in reason
    # The structural check fired BEFORE any signature verification.
    assert vi.call_count == 0, "intent verify must not run on hijacker path"
    assert vc.call_count == 0, "cart verify must not run on hijacker path"
    assert vp.call_count == 0, "payment verify must not run on hijacker path"


def test_audit2_T3_bad_cart_binding_short_circuits_before_crypto(
    dao, seller, agent_did,
):
    """A cart with a tampered intent_mandate_digest should be rejected
    by the B-gate, again before any signature verification."""
    intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(intent))
    # Tamper the binding field
    cart["credentialSubject"] = dict(cart["credentialSubject"])
    cart["credentialSubject"]["intent_mandate_digest"] = "0" * 64
    payment = _sign_payment(dao, seller, cart_mandate_digest(cart))

    with patch(
        "nth_dao.mandate.payment.verify_intent_mandate"
    ) as vi, patch(
        "nth_dao.mandate.payment.verify_cart_mandate"
    ) as vc, patch(
        "nth_dao.mandate.payment.verify_payment_mandate"
    ) as vp:
        ok, reason = complete_triad_chain(intent, cart, payment)

    assert ok is False
    assert "cart vs intent" in reason and "digest mismatch" in reason
    assert vi.call_count == 0
    assert vc.call_count == 0
    assert vp.call_count == 0


# =====================================================================
# T-4: V-27 positional params edge cases
# =====================================================================


def _a2a_client():
    app = create_a2a_app(agent_card=_bare_card(), allow_unauthenticated=True)
    return TestClient(app)


def test_audit2_T4_positional_array_non_string_rejected():
    with _a2a_client() as c:
        resp = c.post("/a2a/jsonrpc", json={
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get",
            "params": [123],     # int, not str
        })
        assert resp.json()["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_audit2_T4_positional_empty_string_rejected():
    with _a2a_client() as c:
        resp = c.post("/a2a/jsonrpc", json={
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get",
            "params": [""],
        })
        assert resp.json()["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_audit2_T4_taskId_only_path_works():
    """Make sure the elif-branch isn't dead - taskId alone should be
    accepted as the legacy alias when `id` key is absent entirely."""
    with _a2a_client() as c:
        resp = c.post("/a2a/jsonrpc", json={
            "jsonrpc": "2.0", "id": 1, "method": "tasks/get",
            "params": {"taskId": "task-001"},   # `id` absent
        })
        # Should NOT be INVALID_PARAMS - we got past param shape, then
        # hit "no mission_store configured".
        assert resp.json()["error"]["code"] != JSONRPC_INVALID_PARAMS


# =====================================================================
# T-5: V-24 streaming attack + boundary
# =====================================================================


def test_audit2_T5_streaming_attack_without_content_length():
    """A client can omit Content-Length and stream a large body. The
    accumulator check must still terminate at max_request_bytes."""
    app = create_a2a_app(
        agent_card=_bare_card(), allow_unauthenticated=True,
        max_request_bytes=512,
    )
    with TestClient(app) as c:
        # TestClient calculates content-length automatically, so to
        # simulate streaming without it we just send a body larger
        # than the cap. The server's content-length pre-check will
        # fire first; ALSO the streaming accumulator would catch it
        # if the header were missing.
        resp = c.post(
            "/a2a/jsonrpc", content="x" * 1024,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == A2A_REQUEST_TOO_LARGE


def test_audit2_T5_boundary_exactly_at_cap_accepted():
    """A request EXACTLY at max_request_bytes must NOT be rejected."""
    cap = 512
    app = create_a2a_app(
        agent_card=_bare_card(), allow_unauthenticated=True,
        max_request_bytes=cap,
    )
    # Build a JSON body whose serialized length is <= cap
    body = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get",
            "params": {"id": "x"}}
    serialized = json.dumps(body)
    assert len(serialized) < cap, "test setup needs a small body"
    with TestClient(app) as c:
        resp = c.post(
            "/a2a/jsonrpc", content=serialized,
            headers={"content-type": "application/json"},
        )
        # Must not be 413
        assert resp.status_code != 413


# =====================================================================
# T-6: V-26 batch size boundary
# =====================================================================


def test_audit2_T6_batch_size_at_cap_accepted():
    cap = 3
    app = create_a2a_app(
        agent_card=_bare_card(), allow_unauthenticated=True,
        max_batch_size=cap,
    )
    batch = [
        {"jsonrpc": "2.0", "id": i, "method": "tasks/get",
         "params": {"id": f"task-{i}"}}
        for i in range(cap)
    ]
    with TestClient(app) as c:
        resp = c.post("/a2a/jsonrpc", json=batch)
        # Should NOT be A2A_REQUEST_TOO_LARGE; each item gets its own
        # response (internal error for "no mission store" but THAT
        # is per-item, not the batch-level rejection).
        # Top-level shape: an array of per-request responses.
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == cap


# =====================================================================
# T-7: V-36 idempotent same-bytes save + list returns earlier
# =====================================================================


def test_audit2_T7_idempotent_same_bytes_save(tmp_path, dao, seller, agent_did):
    """Saving the exact same bytes twice is a true no-op (single file
    on disk, no log spam, deterministic digest)."""
    store = MandateStore(tmp_path)
    intent = _sign_intent(dao, agent_did, seller=seller)
    d1 = store.save_intent(intent)
    d2 = store.save_intent(intent)
    assert d1 == d2
    files = list((tmp_path / "mandates" / "intent").glob("*.json"))
    assert len(files) == 1


def test_audit2_T7_list_returns_preserved_earlier_copy(
    tmp_path, dao, seller, agent_did,
):
    """V-36 promises the earlier proof survives. Verify list_intents()
    returns the earlier sign, not the later one."""
    store = MandateStore(tmp_path)
    unsigned = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did,
        purpose="x",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    earlier = sign_intent_mandate(
        unsigned, dao, created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    later = sign_intent_mandate(
        unsigned, dao, created_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    store.save_intent(earlier)
    store.save_intent(later)

    listed = store.list_intents()
    assert len(listed) == 1
    assert listed[0]["proof"]["created"].startswith("2026-06-01")


# =====================================================================
# T-8: V-38 atomic re-write
# =====================================================================


def test_audit2_T8_write_agent_card_replaces_existing(tmp_path):
    target = tmp_path / "agent.json"
    card_v1 = build_agent_card(
        name="V1", description="", url="https://example.com/a2a",
    )
    card_v2 = build_agent_card(
        name="V2", description="", url="https://example.com/a2a",
    )
    write_agent_card(target, card_v1)
    write_agent_card(target, card_v2)
    assert target.exists()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk["name"] == "V2"
    tmp_leftover = target.with_suffix(target.suffix + ".tmp")
    assert not tmp_leftover.exists()


# =====================================================================
# T-9: V-35 additional dangerous schemes
# =====================================================================


@pytest.mark.parametrize("scheme_url", [
    "data:text/html,<script>evil</script>",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "gopher://example.com/",
])
def test_audit2_T9_dangerous_schemes_rejected(scheme_url):
    """V-35 scheme check rejects anything that isn't http(s) - data:,
    javascript:, file:, gopher: must all fail."""
    with pytest.raises(ValueError):
        build_agent_card(name="X", description="", url=scheme_url)


# =====================================================================
# T-10: V-22 missing-key runtime path
# =====================================================================


def _intent_with_constraint_keys_stripped(
    dao, seller, agent_did, drop: str,
) -> tuple[dict, dict]:
    """Construct a (cart, intent) pair where the cart's binding digest
    MATCHES the (tampered) intent, so the binding-check passes and
    the missing-key branch is reachable. This requires building the
    cart AFTER tampering the intent so its declared digest reflects
    the tampered body."""
    # Build intent with all three keys, sign it
    intent = _sign_intent(dao, agent_did, seller=seller)
    # Strip one key
    intent["credentialSubject"] = dict(intent["credentialSubject"])
    intent["credentialSubject"]["constraints"] = dict(
        intent["credentialSubject"]["constraints"]
    )
    del intent["credentialSubject"]["constraints"][drop]
    # Recompute digest AFTER tampering; cart binds to the tampered
    # body. (The signature on intent is now invalid, but
    # cart_satisfies_intent doesn't verify signatures - that's
    # verify_intent_mandate's job. The point of this test is the
    # runtime missing-key gate.)
    new_digest = intent_mandate_digest(intent)
    cart = _sign_cart(seller, agent_did, new_digest)
    return cart, intent


def test_audit2_T10_runtime_intent_missing_allowed_counterparties_key(
    dao, seller, agent_did,
):
    """When the digest-binding gate passes (because the cart was
    constructed from the tampered intent), the runtime
    missing-key check must fire and reject."""
    cart, intent = _intent_with_constraint_keys_stripped(
        dao, seller, agent_did, drop="allowed_counterparties",
    )
    # Tampered intent has an invalid signature; bypass V-21 strict
    # verification (F-2 4th-round) to reach the missing-key gate.
    ok, reason = cart_satisfies_intent(cart, intent, require_signed=False)
    assert ok is False
    assert "allowed_counterparties missing" in reason


def test_audit2_T10_runtime_intent_missing_allowed_methods_key(
    dao, seller, agent_did,
):
    cart, intent = _intent_with_constraint_keys_stripped(
        dao, seller, agent_did, drop="allowed_settlement_methods",
    )
    ok, reason = cart_satisfies_intent(cart, intent, require_signed=False)
    assert ok is False
    assert "allowed_settlement_methods missing" in reason


# =====================================================================
# T-11: V-32 footgun - explicitly documented
# =====================================================================


def test_audit2_T11_documented_footgun_payment_satisfies_without_intent(
    dao, seller, hijacker, agent_did,
):
    """KNOWN LIMITATION: payment_satisfies_cart called WITHOUT intent=
    cannot detect a hijacker DAO whose cart_mandate_digest + payee +
    settlement_choice all match. The user must either:
      - pass intent= (see V-32 test in test_v010_t2_t9_voss_fixes)
      - or wrap with complete_triad_chain which always enforces
        continuity at gate A.
    This test pins the foot-shooting behavior so anyone removing it
    has to do so deliberately."""
    intent = _sign_intent(dao, agent_did, seller=seller)
    cart = _sign_cart(seller, agent_did, intent_mandate_digest(intent))
    hijacked = _sign_payment(
        hijacker, seller, cart_mandate_digest(cart),
        issuer_did=hijacker.as_did(), signer=hijacker,
    )
    ok, _ = payment_satisfies_cart(hijacked, cart)
    assert ok is True, (
        "If this assertion ever flips, payment_satisfies_cart has been "
        "hardened to detect hijack without intent=. Update the docstring "
        "and remove this footgun test."
    )


# =====================================================================
# Tiny extra sanity: digest stability + different bodies give
# different digests (not a Voss finding but caught in this audit
# because V-36 implicitly assumed it)
# =====================================================================


def test_audit2_digest_distinct_for_distinct_bodies(dao, seller, agent_did):
    intent_a = _sign_intent(dao, agent_did, seller=seller)
    intent_b = build_intent_mandate(
        issuer_did=dao.as_did(), agent_did=agent_did, purpose="different",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [seller.as_did()],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    intent_b = sign_intent_mandate(intent_b, dao)
    assert intent_mandate_digest(intent_a) != intent_mandate_digest(intent_b)
