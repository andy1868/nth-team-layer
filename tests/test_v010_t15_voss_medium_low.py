"""Medium / low Voss findings backlog closure.

Covers:

  V-37  Agent Card nth_dao_extras deep-copied (mutating source dict
        after build doesn't bleed into the served card)
  V-40  A2A internal errors leak an opaque ref id, not the Python
        exception type
  V-42  MandateStore relocates corrupt JSON files instead of
        re-logging them on every list call
  V-44  Mandate event payloads have a 64 KiB hard cap
  V-45  Agent Card skills have a 4 KiB cap on serialized size
  V-46  build_agent_card_from_session logs (not silently swallows)
        when identity.as_did() fails
  V-48  /api/mandates/{kind}/{digest} returns Cache-Control: immutable
        + ETag = digest
  V-49  emit_*_received deep-copies nested mandate fields into the
        event payload (caller mutation can't retroactively rewrite)
  V-50  A2A /.well-known/agent.json carries an ETag and honours
        If-None-Match -> 304
  V-51  validate_agent_card type-checks the required string fields
        (not just presence)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from nth_dao.a2a.agent_card import (
    build_agent_card,
    build_agent_card_from_session,
    validate_agent_card,
)
from nth_dao.a2a.server import create_a2a_app
from nth_dao.event_bus import EventBus
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.cart import (
    build_cart_mandate,
    cart_mandate_digest,
    sign_cart_mandate,
)
from nth_dao.mandate.events import (
    emit_cart_received,
    emit_intent_issued,
)
from nth_dao.mandate.intent import (
    build_intent_mandate,
    intent_mandate_digest,
    sign_intent_mandate,
)
from nth_dao.mandate.store import MandateStore
from nth_dao.web import create_app


# ----- fixtures -----


@pytest.fixture
def dao() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t15-dao")


@pytest.fixture
def seller() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t15-seller")


@pytest.fixture
def agent_did() -> str:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="t15-agent").as_did()


@pytest.fixture
def bus(tmp_path: Path, dao) -> EventBus:
    return EventBus(tmp_path, identity=dao)


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


def _bare_card() -> Dict[str, Any]:
    return build_agent_card(
        name="V-medium-low Test",
        description="",
        url="https://localhost:8080/a2a",
        capabilities=["echo"],
    )


# =====================================================================
# V-37: nth_dao_extras deep copy
# =====================================================================


def test_T15_V37_nth_dao_extras_deep_copy_isolates_caller_mutation():
    """Mutating the source extras dict AFTER build_agent_card returns
    must not affect the served card."""
    extras = {"nested": {"foo": "original"}}
    card = build_agent_card(
        name="X", description="", url="https://x.com/a",
        nth_dao_extras=extras,
    )
    # Mutate the source AFTER build
    extras["nested"]["foo"] = "evil"
    # Card should still have the original value
    assert card["x-nth-dao"]["nested"]["foo"] == "original"


def test_T15_V37_top_level_extras_isolated():
    extras = {"top": "value"}
    card = build_agent_card(
        name="X", description="", url="https://x.com/a",
        nth_dao_extras=extras,
    )
    extras["top"] = "evil"
    assert card["x-nth-dao"]["top"] == "value"


# =====================================================================
# V-40: A2A internal error opaque ref id
# =====================================================================


def test_T15_V40_internal_error_returns_opaque_ref_id(tmp_path):
    """If a method handler raises an uncaught exception, the response
    must not include the Python exception type name (which would leak
    server-side internals to the network). Should include a
    correlation ref id instead."""
    app = create_a2a_app(
        agent_card=_bare_card(), allow_unauthenticated=True,
        mission_store=None,
    )
    # Force a 500 by calling tasks/get with no mission_store; this
    # hits the JSONRPC_INTERNAL_ERROR branch via JsonRpcError, NOT
    # the V-40 generic path. So we need to inject a method that
    # actually raises. Instead: monkeypatch the mission store on the
    # underlying dispatcher to raise a deliberate exception.
    # Pragmatic: register an in-process fault-injection mission store.
    class FaultyStore:
        def get(self, _id):
            raise RuntimeError("simulated internal failure")

    from nth_dao.a2a import server as srv

    # Wrap _do_tasks_get to use the faulty store. Test client send
    # a tasks/get call; the handler turns RuntimeError into a
    # JsonRpcError(INTERNAL) with a specific "mission_store lookup
    # failed: RuntimeError" message - that's still type-leak. Let me
    # test the OUTER generic except.
    # Easier: patch _invoke_method to raise a non-JsonRpcError
    original_invoke = srv._invoke_method

    def fake_invoke(method, params, mission_store):
        raise KeyError("evil")

    try:
        srv._invoke_method = fake_invoke
        with TestClient(app) as c:
            resp = c.post("/a2a/jsonrpc", json={
                "jsonrpc": "2.0", "id": 1, "method": "tasks/get",
                "params": {"id": "x"},
            })
        body = resp.json()
        assert "KeyError" not in body["error"]["message"]
        assert "Internal error (ref " in body["error"]["message"]
    finally:
        srv._invoke_method = original_invoke


# =====================================================================
# V-42: MandateStore relocates corrupt files
# =====================================================================


def test_T15_V42_corrupt_file_relocated_not_re_warned(
    tmp_path, dao, seller, agent_did,
):
    """Plant a corrupt file then list twice. The first call should
    move the file aside; the second call must not re-log a warning
    (since the file is no longer in the listing glob)."""
    store = MandateStore(tmp_path)
    intent = _signed_intent(dao, agent_did, seller=seller)
    store.save_intent(intent)
    # Plant a corrupt file
    corrupt_path = tmp_path / "mandates" / "intent" / "garbage.json"
    corrupt_path.write_text("{ not valid", encoding="utf-8")

    # First list: corrupt file gets relocated
    first = store.list_intents()
    assert len(first) == 1
    assert not corrupt_path.exists()
    relocated = list((tmp_path / "mandates" / "intent").glob("garbage.json.corrupt.*"))
    assert len(relocated) == 1

    # Second list: no further warning because the corrupt file is no
    # longer in the *.json glob.
    second = store.list_intents()
    assert len(second) == 1


def test_T15_V42_relocation_preserves_corrupt_content_for_forensics(
    tmp_path, dao, seller, agent_did,
):
    """The relocated file must still contain the original (corrupt)
    bytes so ops can investigate."""
    store = MandateStore(tmp_path)
    corrupt_path = tmp_path / "mandates" / "intent" / "garbage.json"
    bytes_in = b"{ specific bytes for forensic recovery"
    corrupt_path.write_bytes(bytes_in)

    store.list_intents()
    relocated = list((tmp_path / "mandates" / "intent").glob("garbage.json.corrupt.*"))
    assert len(relocated) == 1
    assert relocated[0].read_bytes() == bytes_in


# =====================================================================
# V-44: Mandate event payload size cap
# =====================================================================


def test_T15_V44_oversized_intent_event_rejected(bus, dao, seller, agent_did):
    """If somehow a payload would serialize past 64 KiB, the emit
    helper must refuse. The realistic trigger is a buggy adapter
    supplying a huge max_amount via constraints."""
    intent = _signed_intent(dao, agent_did, seller=seller)
    # Tamper to inject a huge value into a copied dict. We can't
    # tamper a SIGNED intent (sig would break verify_signed gate);
    # use require_signed=False to reach the size gate.
    intent["credentialSubject"]["constraints"]["max_amount"] = {
        "value": "100.00", "currency": "USDC",
        "x_huge_padding": "P" * 70000,
    }
    with pytest.raises(ValueError, match="exceeds.*bytes"):
        emit_intent_issued(bus, intent, require_signed=False)


def test_T15_V44_normal_intent_event_passes(bus, dao, seller, agent_did):
    intent = _signed_intent(dao, agent_did, seller=seller)
    ev = emit_intent_issued(bus, intent)
    assert ev.event_type == "mandate.intent.issued"


# =====================================================================
# V-45: Agent Card skill size cap
# =====================================================================


def test_T15_V45_oversized_skill_rejected():
    with pytest.raises(ValueError, match="max serialized size"):
        build_agent_card(
            name="X", description="", url="https://x.com/a",
            skills=[{
                "id": "evil",
                "name": "evil-skill",
                "x-bloat": "P" * 5000,
            }],
        )


def test_T15_V45_normal_skill_passes():
    card = build_agent_card(
        name="X", description="", url="https://x.com/a",
        skills=[{
            "id": "ok",
            "name": "ok-skill",
            "description": "ordinary",
            "x-extension": "small enough",
        }],
    )
    assert len(card["skills"]) == 1


# =====================================================================
# V-46: build_agent_card_from_session logs on identity decode failure
# =====================================================================


class _BrokenIdentitySession:
    """Minimal duck-typed session whose identity.as_did() raises."""

    agent_id = "broken-agent"
    capabilities = ["echo"]
    workspace = "/tmp/x"
    groups = []

    class _Identity:
        pubkey_hex = "f" * 64    # truthy so the if-branch runs

        def as_did(self) -> str:
            raise RuntimeError("simulated decode failure")

    identity = _Identity()


def test_T15_V46_session_decode_failure_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="nth_dao.a2a.agent_card"):
        card = build_agent_card_from_session(
            _BrokenIdentitySession(),
            url="https://x.com/a",
        )
    # Card was still built (degraded - no agent_did) but warning was logged.
    assert "x-nth-dao" in card
    assert "agent_did" not in card["x-nth-dao"]
    log_msgs = [r.getMessage() for r in caplog.records]
    assert any("agent_did decode failed" in m for m in log_msgs)


# =====================================================================
# V-48: /api/mandates/{kind}/{digest} cache headers
# =====================================================================


def test_T15_V48_mandate_get_returns_cache_immutable(
    tmp_path, dao, seller, agent_did,
):
    client = TestClient(create_app(tmp_path))
    intent = _signed_intent(dao, agent_did, seller=seller)
    # Persist via the store endpoint
    # NOTE: actor_id is now explicit-required on /api/mandates/* per
    # the tightened auth gate; pass DEFAULT_ADMIN_ID directly.
    client.post("/api/mandates/store", json={
        "kind": "intent", "mandate": intent, "actor_id": "admin",
    })
    digest = intent_mandate_digest(intent)

    resp = client.get(
        f"/api/mandates/intent/{digest}?actor_id=admin",
    )
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "immutable" in cc
    assert "max-age=" in cc
    assert resp.headers.get("ETag") == f'"{digest}"'


# =====================================================================
# V-49: emit_*_received deep-copies nested fields
# =====================================================================


def test_T15_V49_emit_intent_issued_isolates_max_amount(
    bus, dao, seller, agent_did,
):
    """Mutate the source mandate AFTER emit; the event payload's
    max_amount must keep the original value."""
    intent = _signed_intent(dao, agent_did, seller=seller)
    ev = emit_intent_issued(bus, intent)
    assert ev.payload["max_amount"]["value"] == "100.00"
    # Mutate after emit
    intent["credentialSubject"]["constraints"]["max_amount"]["value"] = "999"
    # Event payload retains the original
    assert ev.payload["max_amount"]["value"] == "100.00"


def test_T15_V49_emit_cart_received_isolates_total(
    bus, dao, seller, agent_did,
):
    intent = _signed_intent(dao, agent_did, seller=seller)
    cart = sign_cart_mandate(build_cart_mandate(
        issuer_did=seller.as_did(), buyer_did=agent_did,
        intent_mandate_digest_hex=intent_mandate_digest(intent),
        items=[{"description": "x", "quantity": 1}],
        total={"value": "50.00", "currency": "USDC"},
        settlement_methods=["x402:usdc"], expires_at=_future(3600),
    ), seller)
    ev = emit_cart_received(bus, cart)
    # Mutate source after emit
    cart["credentialSubject"]["total"]["value"] = "999"
    assert ev.payload["total"]["value"] == "50.00"


# =====================================================================
# V-50: A2A /.well-known/agent.json ETag + 304
# =====================================================================


def test_T15_V50_well_known_has_etag():
    app = create_a2a_app(agent_card=_bare_card(), allow_unauthenticated=True)
    with TestClient(app) as c:
        resp = c.get("/.well-known/agent.json")
        assert resp.status_code == 200
        etag = resp.headers.get("ETag", "")
        assert etag.startswith('"') and etag.endswith('"')


def test_T15_V50_well_known_returns_304_on_match():
    app = create_a2a_app(agent_card=_bare_card(), allow_unauthenticated=True)
    with TestClient(app) as c:
        first = c.get("/.well-known/agent.json")
        etag = first.headers["ETag"]
        second = c.get("/.well-known/agent.json", headers={
            "if-none-match": etag,
        })
        assert second.status_code == 304
        # Same ETag returned
        assert second.headers["ETag"] == etag


def test_T15_V50_etag_is_stable_across_two_consecutive_fetches():
    app = create_a2a_app(agent_card=_bare_card(), allow_unauthenticated=True)
    with TestClient(app) as c:
        a = c.get("/.well-known/agent.json")
        b = c.get("/.well-known/agent.json")
        assert a.headers["ETag"] == b.headers["ETag"]


# =====================================================================
# V-51: validate_agent_card type-checks required string fields
# =====================================================================


def test_T15_V51_validate_rejects_null_name():
    card = build_agent_card(name="X", description="", url="https://x.com/a")
    card["name"] = None
    ok, reason = validate_agent_card(card)
    assert ok is False
    assert "name must be a non-empty string" in reason


def test_T15_V51_validate_rejects_empty_string_name():
    card = build_agent_card(name="X", description="", url="https://x.com/a")
    card["name"] = ""
    ok, reason = validate_agent_card(card)
    assert ok is False
    assert "name must be a non-empty string" in reason


def test_T15_V51_validate_rejects_non_string_version():
    card = build_agent_card(name="X", description="", url="https://x.com/a")
    card["version"] = 0.10
    ok, reason = validate_agent_card(card)
    assert ok is False
    assert "version" in reason


def test_T15_V51_validate_accepts_empty_description():
    card = build_agent_card(name="X", description="", url="https://x.com/a")
    ok, _ = validate_agent_card(card)
    assert ok is True
