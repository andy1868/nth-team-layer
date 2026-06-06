"""Tests for the v0.10 Sprint Zero T-9 Mandate sidebar backend.

T-9 introduces the file-backed ``MandateStore`` and four web routes
that feed the React console's Mandate sidebar:

    GET  /api/mandates                      - list summary rows
    GET  /api/mandates/{kind}/{digest}      - fetch the full body
    POST /api/mandates/store                - persist a signed body
    POST /api/mandates/verify               - signature + binding check

The sidebar reads ``/api/mandates`` once on mount, renders three
collapsible sections, and offers a per-row Verify button that POSTs
to ``/api/mandates/verify``. Issuing a new IntentMandate goes
through ``/api/mandates/store`` after the browser wallet has signed
the canonical JSON.

These tests pin the on-the-wire shape so the TypeScript client in
``frontend/src/api.ts`` can rely on it. They also guard against the
classic two failure modes of a thin Mandate dashboard:

    1. Forging an index entry that points to the wrong digest
       (mitigated: server re-derives the digest from the body).
    2. Letting a stale or wrongly-purposed mandate render as valid
       (mitigated: ``/verify`` runs full signature check + expiry
       gate + optional binding gate in one shot).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.membership import MembershipManager, TeamRole
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    sign_cart_mandate,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.payment import (
    build_payment_mandate,
    payment_mandate_digest,
    sign_payment_mandate,
)
from nth_dao.mandate.store import (
    KIND_CART,
    KIND_INTENT,
    KIND_PAYMENT,
    MandateStore,
)
from nth_dao.web import create_app


ACTOR_ID = "admin"
ACTOR_QS = f"?actor_id={ACTOR_ID}"


# ===== fixtures =====


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="t9-dao")


@pytest.fixture
def counterparty() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="t9-shop")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required for signed mandates")
    return AgentIdentity.generate(label="t9-agent").as_did()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _signed_intent(dao: AgentIdentity, agent_did: str, **overrides) -> dict:
    # Voss V-22: empty allowed_counterparties is fail-closed; the
    # helper accepts an optional whitelist_counterparty kwarg to
    # populate it.
    whitelist_did = overrides.pop("whitelist_counterparty_did", None)
    constraints = overrides.pop("constraints", None) or {
        "max_amount": {"value": "100.00", "currency": "USDC"},
        "allowed_counterparties": (
            [whitelist_did] if whitelist_did is not None else []
        ),
        "allowed_settlement_methods": ["x402:usdc", "ap2:card"],
    }
    intent = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose=overrides.pop("purpose", "buy code review"),
        constraints=constraints,
        expires_at=overrides.pop("expires_at", _future(86400)),
        **overrides,
    )
    return sign_intent_mandate(intent, dao)


def _signed_cart(
    counterparty: AgentIdentity,
    agent_did: str,
    intent_digest: str,
    **overrides,
) -> dict:
    cart = build_cart_mandate(
        issuer_did=counterparty.as_did(),
        buyer_did=agent_did,
        intent_mandate_digest_hex=intent_digest,
        items=[{"description": "PR review", "quantity": 1}],
        total=overrides.pop("total", {"value": "50.00", "currency": "USDC"}),
        settlement_methods=overrides.pop("settlement_methods", ["x402:usdc"]),
        expires_at=overrides.pop("expires_at", _future(3600)),
        **overrides,
    )
    return sign_cart_mandate(cart, counterparty)


def _signed_payment(
    dao: AgentIdentity,
    counterparty: AgentIdentity,
    cart_digest: str,
    **overrides,
) -> dict:
    payment = build_payment_mandate(
        issuer_did=dao.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_digest,
        settlement_choice=overrides.pop("settlement_choice", "x402:usdc"),
        expires_at=overrides.pop("expires_at", _future(900)),
        **overrides,
    )
    return sign_payment_mandate(payment, dao)


# ===== 1. MandateStore unit tests =====


def test_T9_01_store_creates_kind_subdirs(tmp_path):
    store = MandateStore(tmp_path)
    assert (tmp_path / "mandates" / KIND_INTENT).is_dir()
    assert (tmp_path / "mandates" / KIND_CART).is_dir()
    assert (tmp_path / "mandates" / KIND_PAYMENT).is_dir()


def test_T9_02_store_save_returns_canonical_digest(tmp_path, dao, agent_did):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    expected = intent_mandate_digest(intent)
    actual = store.save_intent(intent)
    assert actual == expected
    assert (tmp_path / "mandates" / KIND_INTENT / f"{actual}.json").exists()


def test_T9_03_store_save_idempotent_under_same_digest(tmp_path, dao, agent_did):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    digest_one = store.save_intent(intent)
    digest_two = store.save_intent(intent)
    assert digest_one == digest_two
    # only one file on disk despite two writes
    files = list((tmp_path / "mandates" / KIND_INTENT).glob("*.json"))
    assert len(files) == 1


def test_T9_03b_store_save_same_digest_race_writes_once(
    tmp_path, dao, agent_did, monkeypatch,
):
    """Same-digest mandates with different proof timestamps must not
    race past path.exists() and overwrite each other."""
    import nth_dao.mandate.store as store_module

    store = MandateStore(tmp_path)
    unsigned = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="buy code review",
        constraints={
            "max_amount": {"value": "100.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_future(86400),
    )
    first = sign_intent_mandate(
        unsigned, dao,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    second = sign_intent_mandate(
        unsigned, dao,
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    original_write = store_module.atomic_write_json
    write_count = 0
    write_entered = threading.Event()
    release_first_write = threading.Event()
    write_lock = threading.Lock()

    def slow_write(path, data):
        nonlocal write_count
        with write_lock:
            write_count += 1
            current = write_count
        if current == 1:
            write_entered.set()
            assert release_first_write.wait(timeout=5)
        return original_write(path, data)

    monkeypatch.setattr(store_module, "atomic_write_json", slow_write)

    errors = []

    def save(mandate):
        try:
            store.save_intent(mandate)
        except Exception as exc:  # pragma: no cover - test diagnostic
            errors.append(exc)

    t1 = threading.Thread(target=save, args=(first,))
    t2 = threading.Thread(target=save, args=(second,))
    t1.start()
    assert write_entered.wait(timeout=5)
    t2.start()
    release_first_write.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors
    assert write_count == 1
    digest = intent_mandate_digest(first)
    saved = store.get(KIND_INTENT, digest)
    assert saved["proof"]["created"] == first["proof"]["created"]


def test_T9_03c_store_save_replaces_corrupt_same_digest_file(
    tmp_path, dao, agent_did
):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    path = tmp_path / "mandates" / KIND_INTENT / f"{digest}.json"
    path.write_text("{ broken", encoding="utf-8")

    assert store.save_intent(intent) == digest

    saved = store.get(KIND_INTENT, digest)
    assert saved == intent
    relocated = list(
        (tmp_path / "mandates" / KIND_INTENT).glob(f"{digest}.json.corrupt.*")
    )
    assert len(relocated) == 1
    assert relocated[0].read_text(encoding="utf-8") == "{ broken"


def test_T9_04_store_get_returns_full_body(tmp_path, dao, agent_did):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    digest = store.save_intent(intent)
    loaded = store.get(KIND_INTENT, digest)
    assert loaded is not None
    assert loaded["@context"] == intent["@context"]
    assert loaded["proof"]["proofValue"] == intent["proof"]["proofValue"]


def test_T9_05_store_get_missing_returns_none(tmp_path):
    store = MandateStore(tmp_path)
    missing_digest = "0" * 64
    assert store.get(KIND_INTENT, missing_digest) is None


def test_T9_06_store_get_rejects_unknown_kind(tmp_path):
    store = MandateStore(tmp_path)
    with pytest.raises(ValueError, match="unknown mandate kind"):
        store.get("settlement", "0" * 64)


def test_T9_07_store_get_rejects_bad_digest_shape(tmp_path):
    store = MandateStore(tmp_path)
    with pytest.raises(ValueError, match="64-hex"):
        store.get(KIND_INTENT, "abc")
    with pytest.raises(ValueError, match="64-hex"):
        store.get(KIND_INTENT, None)  # type: ignore[arg-type]


def test_T9_08_store_list_returns_all_kinds(tmp_path, dao, counterparty, agent_did):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    intent_digest = store.save_intent(intent)
    cart = _signed_cart(counterparty, agent_did, intent_digest)
    cart_digest = store.save_cart(cart)
    payment = _signed_payment(dao, counterparty, cart_digest)
    store.save_payment(payment)

    assert len(store.list_intents()) == 1
    assert len(store.list_carts()) == 1
    assert len(store.list_payments()) == 1


def test_T9_09_store_list_skips_corrupt_files(tmp_path, dao, agent_did):
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did)
    store.save_intent(intent)
    # plant a corrupt file
    (tmp_path / "mandates" / KIND_INTENT / "garbage.json").write_text(
        "{ not valid json", encoding="utf-8"
    )
    listing = store.list_intents()
    # corrupt file is silently dropped, valid mandate remains
    assert len(listing) == 1


# ===== 2. /api/mandates listing route =====


def test_T9_10_listing_returns_three_empty_arrays_when_no_mandates(client):
    response = client.get(f"/api/mandates{ACTOR_QS}")
    assert response.status_code == 200
    body = response.json()
    assert body == {"intents": [], "carts": [], "payments": []}


def test_T9_10b_listing_requires_explicit_actor(client):
    response = client.get("/api/mandates")
    assert response.status_code == 422


def test_T9_10d_listing_rejects_non_member_without_auto_join(client, tmp_path):
    response = client.get("/api/mandates?actor_id=stranger")
    assert response.status_code == 403
    config = MembershipManager(tmp_path).load_config()
    assert config.role_for("stranger") == TeamRole.GUEST


def test_T9_10c_all_mandate_routes_require_explicit_actor(client, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)

    assert client.get(f"/api/mandates/{KIND_INTENT}/{digest}").status_code == 422
    assert client.post(
        "/api/mandates/store",
        json={"kind": KIND_INTENT, "mandate": intent},
    ).status_code == 422
    assert client.post(
        "/api/mandates/verify",
        json={"kind": KIND_INTENT, "mandate": intent},
    ).status_code == 422


def test_T9_11_listing_includes_summary_fields_per_intent(
    client, tmp_path, dao, agent_did
):
    intent = _signed_intent(dao, agent_did)
    expected_digest = intent_mandate_digest(intent)
    # seed the store directly using the same workspace as the test client
    MandateStore(tmp_path).save_intent(intent)

    response = client.get(f"/api/mandates{ACTOR_QS}")
    assert response.status_code == 200
    summaries = response.json()["intents"]
    assert len(summaries) == 1
    row = summaries[0]
    assert row["kind"] == KIND_INTENT
    assert row["digest"] == expected_digest
    assert row["issuer"] == dao.as_did()
    assert row["agent"] == agent_did
    assert row["purpose"] == "buy code review"
    assert row["max_amount"]["currency"] == "USDC"
    assert row["max_amount"]["value"] == "100.00"
    assert row["expired"] is False
    assert row["allowed_settlement_methods"] == ["x402:usdc", "ap2:card"]


def test_T9_12_listing_flags_expired_intents(client, tmp_path, dao, agent_did):
    # set a validUntil already in the past. The build-time gate
    # rejects validUntil <= issuanceDate (Voss V-12), so the issuance
    # has to be backdated even further to construct a legitimately
    # issued-then-expired mandate.
    expired_intent_unsigned = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="stale",
        constraints={
            "max_amount": {"value": "10.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_past(60),
        issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    intent = sign_intent_mandate(expired_intent_unsigned, dao)
    MandateStore(tmp_path).save_intent(intent)

    summaries = client.get(f"/api/mandates{ACTOR_QS}").json()["intents"]
    assert summaries[0]["expired"] is True


def test_T9_13_listing_summarises_cart_with_intent_binding(
    client, tmp_path, dao, counterparty, agent_did
):
    intent = _signed_intent(dao, agent_did)
    intent_digest = intent_mandate_digest(intent)
    cart = _signed_cart(counterparty, agent_did, intent_digest)
    MandateStore(tmp_path).save_cart(cart)

    summaries = client.get(f"/api/mandates{ACTOR_QS}").json()["carts"]
    assert len(summaries) == 1
    row = summaries[0]
    assert row["kind"] == KIND_CART
    assert row["digest"] == cart_mandate_digest(cart)
    assert row["intent_digest"] == intent_digest
    assert row["issuer"] == counterparty.as_did()
    assert row["total"] == {"currency": "USDC", "value": "50.00"}
    assert row["settlement_methods"] == ["x402:usdc"]
    assert row["line_item_count"] == 1
    assert row["expired"] is False


def test_T9_14_listing_summarises_payment_with_cart_binding(
    client, tmp_path, dao, counterparty, agent_did
):
    intent = _signed_intent(dao, agent_did)
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    cart_digest = cart_mandate_digest(cart)
    payment = _signed_payment(dao, counterparty, cart_digest)
    MandateStore(tmp_path).save_payment(payment)

    summaries = client.get(f"/api/mandates{ACTOR_QS}").json()["payments"]
    assert len(summaries) == 1
    row = summaries[0]
    assert row["kind"] == KIND_PAYMENT
    assert row["digest"] == payment_mandate_digest(payment)
    assert row["cart_digest"] == cart_digest
    assert row["issuer"] == dao.as_did()
    assert row["payee"] == counterparty.as_did()
    assert row["settlement_choice"] == "x402:usdc"
    assert row["issued_at"] != ""
    assert row["expired"] is False


# ===== 3. /api/mandates/{kind}/{digest} fetch route =====


def test_T9_15_fetch_returns_full_body(client, tmp_path, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    digest = intent_mandate_digest(intent)
    MandateStore(tmp_path).save_intent(intent)

    response = client.get(f"/api/mandates/{KIND_INTENT}/{digest}{ACTOR_QS}")
    assert response.status_code == 200
    body = response.json()
    assert body["@context"] == intent["@context"]
    assert body["proof"]["proofValue"] == intent["proof"]["proofValue"]


def test_T9_16_fetch_404_when_missing(client):
    missing = "0" * 64
    response = client.get(f"/api/mandates/{KIND_INTENT}/{missing}{ACTOR_QS}")
    assert response.status_code == 404


def test_T9_17_fetch_400_when_bad_kind(client):
    response = client.get(f"/api/mandates/settlement/{'0' * 64}{ACTOR_QS}")
    assert response.status_code == 400


def test_T9_18_fetch_400_when_bad_digest_shape(client):
    response = client.get(f"/api/mandates/{KIND_INTENT}/abc{ACTOR_QS}")
    assert response.status_code == 400


# ===== 4. /api/mandates/store persist route =====


def test_T9_19_store_route_persists_and_returns_digest(client, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    expected_digest = intent_mandate_digest(intent)
    response = client.post(
        "/api/mandates/store",
        json={"kind": KIND_INTENT, "mandate": intent, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["kind"] == KIND_INTENT
    assert body["digest"] == expected_digest

    # listing reflects the stored mandate
    listing = client.get(f"/api/mandates{ACTOR_QS}").json()
    assert len(listing["intents"]) == 1
    assert listing["intents"][0]["digest"] == expected_digest


def test_T9_20_store_route_rejects_unknown_kind(client, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    response = client.post(
        "/api/mandates/store",
        json={"kind": "settlement", "mandate": intent, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 400


def test_T9_21_store_route_rejects_malformed_body(client):
    response = client.post(
        "/api/mandates/store",
        json={"kind": KIND_INTENT, "mandate": {"junk": True}, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 400


# ===== 5. /api/mandates/verify =====


def test_T9_22_verify_intent_happy_path(client, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    response = client.post(
        "/api/mandates/verify",
        json={"kind": KIND_INTENT, "mandate": intent, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    names = {check["name"] for check in body["checks"]}
    assert "signature" in names
    assert "expiry" in names


def test_T9_23_verify_intent_detects_tampering(client, dao, agent_did):
    """Mutating a signed field must invalidate the signature.

    The constraint sits at ``credentialSubject.constraints.max_amount``
    not at top-level - editing the wrong path would leave the signed
    canonical JSON intact and let the test pass for the wrong reason.
    """
    intent = _signed_intent(dao, agent_did)
    tampered = dict(intent)
    tampered_subject = dict(tampered["credentialSubject"])
    tampered_constraints = dict(tampered_subject["constraints"])
    tampered_constraints["max_amount"] = {"value": "9999.00", "currency": "USDC"}
    tampered_subject["constraints"] = tampered_constraints
    tampered["credentialSubject"] = tampered_subject

    response = client.post(
        "/api/mandates/verify",
        json={"kind": KIND_INTENT, "mandate": tampered, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "signature" in body["reason"].lower()


def test_T9_24_verify_intent_flags_expired(client, dao, agent_did):
    expired_unsigned = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent_did,
        purpose="stale",
        constraints={
            "max_amount": {"value": "10.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=_past(60),
        issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    expired = sign_intent_mandate(expired_unsigned, dao)
    response = client.post(
        "/api/mandates/verify",
        json={"kind": KIND_INTENT, "mandate": expired, "actor_id": ACTOR_ID},
    )
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "expired"


def test_T9_25_verify_cart_with_intent_binding(
    client, dao, counterparty, agent_did
):
    # V-22: must whitelist the counterparty or cart_satisfies_intent
    # fails closed on the empty list.
    intent = _signed_intent(
        dao, agent_did,
        whitelist_counterparty_did=counterparty.as_did(),
    )
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_CART,
            "mandate": cart,
            "against_intent": intent,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is True
    names = {check["name"] for check in body["checks"]}
    assert "binds_intent" in names


def test_T9_26_verify_cart_rejects_wrong_intent_binding(
    client, dao, counterparty, agent_did
):
    """A cart that binds to intent A must not validate against intent B."""
    intent_a = _signed_intent(dao, agent_did)
    intent_b = _signed_intent(dao, agent_did, purpose="different purpose")
    # cart binds to A
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent_a))

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_CART,
            "mandate": cart,
            "against_intent": intent_b,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is False
    assert "digest" in body["reason"].lower() or "intent" in body["reason"].lower()


def test_T9_27_verify_payment_with_cart_binding(
    client, dao, counterparty, agent_did
):
    intent = _signed_intent(
        dao,
        agent_did,
        whitelist_counterparty_did=counterparty.as_did(),
    )
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, counterparty, cart_mandate_digest(cart))

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_PAYMENT,
            "mandate": payment,
            "against_intent": intent,
            "against_cart": cart,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is True
    names = {check["name"] for check in body["checks"]}
    assert "complete_triad" in names


def test_T9_27b_verify_payment_with_cart_requires_intent(
    client, dao, counterparty, agent_did
):
    intent = _signed_intent(
        dao,
        agent_did,
        whitelist_counterparty_did=counterparty.as_did(),
    )
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, counterparty, cart_mandate_digest(cart))

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_PAYMENT,
            "mandate": payment,
            "against_cart": cart,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is False
    assert "against_intent" in body["reason"]
    names = {check["name"] for check in body["checks"]}
    assert "complete_triad" in names


def test_T9_27d_verify_payment_rejects_signature_only_request(
    client, dao, counterparty, agent_did
):
    intent = _signed_intent(
        dao,
        agent_did,
        whitelist_counterparty_did=counterparty.as_did(),
    )
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = _signed_payment(dao, counterparty, cart_mandate_digest(cart))

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_PAYMENT,
            "mandate": payment,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is False
    assert "against_intent" in body["reason"]
    assert "against_cart" in body["reason"]


def test_T9_27c_verify_payment_rejects_hijacked_triad(
    client, dao, counterparty, agent_did
):
    hijacker = AgentIdentity.generate(label="t9-hijacker")
    intent = _signed_intent(
        dao,
        agent_did,
        whitelist_counterparty_did=counterparty.as_did(),
    )
    cart = _signed_cart(counterparty, agent_did, intent_mandate_digest(intent))
    payment = build_payment_mandate(
        issuer_did=hijacker.as_did(),
        payee_did=counterparty.as_did(),
        cart_mandate_digest_hex=cart_mandate_digest(cart),
        settlement_choice="x402:usdc",
        expires_at=_future(900),
    )
    payment = sign_payment_mandate(payment, hijacker)

    response = client.post(
        "/api/mandates/verify",
        json={
            "kind": KIND_PAYMENT,
            "mandate": payment,
            "against_intent": intent,
            "against_cart": cart,
            "actor_id": ACTOR_ID,
        },
    )
    body = response.json()
    assert body["ok"] is False
    assert "issuer continuity broken" in body["reason"]


def test_T9_28_verify_route_rejects_unknown_kind(client, dao, agent_did):
    intent = _signed_intent(dao, agent_did)
    response = client.post(
        "/api/mandates/verify",
        json={"kind": "settlement", "mandate": intent, "actor_id": ACTOR_ID},
    )
    assert response.status_code == 400


def test_T9_29_verify_route_returns_malformed_reason_for_garbage_body(client):
    response = client.post(
        "/api/mandates/verify",
        json={"kind": KIND_INTENT, "mandate": {"junk": True}, "actor_id": ACTOR_ID},
    )
    # malformed body is a soft fail (ok=false), not a 400 - the sidebar
    # surfaces the reason in the row badge rather than as an HTTP error
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "malformed" in body["reason"]
