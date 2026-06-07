"""Architect R-5 (2026-06-07): /api/agents/lan_discover hardening.

Pre-fix this endpoint:
  * Had no actor_id gate - anyone reachable could trigger UDP
    broadcasts through the server
  * Hard-coded the querier identity to DEFAULT_ADMIN_ID, so the
    LAN saw the server impersonating its admin
  * Pulled the PSK from the request body, letting a caller probe
    accepted PSK values one at a time
  * Had no rate limit, so a small POST -> 6s of UDP-amplified
    response traffic was an attack-cost asymmetry

Pins:
  * actor_id is required (400 without)
  * non-member actor -> 403
  * PSK arrives via NTH_DISCOVERY_PSK env, never from the request
  * a rate limit fires after the documented window
  * the limit is per-actor (one noisy actor doesn't starve others)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nth_dao.web as web_mod
from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # Reset the module-level limiter so each test starts clean.
    web_mod._lan_discover_limiter.reset()
    return TestClient(create_app(tmp_path))


def _stub_lan_discovery():
    """Patch LANDiscovery so tests never actually broadcast UDP."""
    class _NoOpLAN:
        def __init__(self, *_, **__): pass
        def discover(self, *_, **__): return []
    return patch.object(web_mod, "LANDiscovery", _NoOpLAN)


# ===== auth gate =====


def test_R5_lan_discover_requires_actor_id(client):
    with _stub_lan_discovery():
        resp = client.post("/api/agents/lan_discover", json={})
    assert resp.status_code == 400
    assert "actor_id" in resp.json()["detail"]


def test_R5_lan_discover_rejects_non_member_actor(client):
    with _stub_lan_discovery():
        resp = client.post(
            "/api/agents/lan_discover",
            json={"actor_id": "stranger"},
        )
    assert resp.status_code == 403


def test_R5_lan_discover_succeeds_for_admin(client):
    with _stub_lan_discovery():
        resp = client.post(
            "/api/agents/lan_discover",
            json={"actor_id": "admin", "timeout_seconds": 0.5},
        )
    assert resp.status_code == 200
    assert resp.json() == {"peers": []}


# ===== PSK comes from env, not request =====


def test_R5_psk_from_request_body_is_ignored(client, monkeypatch):
    """A payload-supplied PSK must NOT reach the discovery client.
    The server's PSK is read from NTH_DISCOVERY_PSK only."""
    captured_psk = []

    class _CapturingLAN:
        def __init__(self, *, agent_id, psk, **_extra):
            # LAN DID publish (2026-06-07): the endpoint now also
            # passes did + pubkey_hex; tolerate them via **_extra so
            # this test continues to focus on PSK provenance.
            captured_psk.append(psk)
            self.agent_id = agent_id
        def discover(self, **_):
            return []

    monkeypatch.setenv("NTH_DISCOVERY_PSK", "server-secret")
    with patch.object(web_mod, "LANDiscovery", _CapturingLAN):
        resp = client.post(
            "/api/agents/lan_discover",
            json={
                "actor_id": "admin",
                "timeout_seconds": 0.5,
                # An attacker tries to probe by including their guess.
                # Pydantic should silently ignore unknown fields, OR
                # accept the field and the server should discard it.
                "psk": "attacker-guess",
            },
        )
    assert resp.status_code == 200
    assert captured_psk == ["server-secret"], (
        f"server used PSK from request body instead of env: {captured_psk}"
    )


def test_R5_querier_agent_id_is_actor_not_admin(client):
    """The querier identity broadcast on the LAN is the calling
    actor's id, NOT a hard-coded DEFAULT_ADMIN_ID."""
    captured_agent = []

    class _CapturingLAN:
        def __init__(self, *, agent_id, psk, **_extra):
            captured_agent.append(agent_id)
        def discover(self, **_):
            return []

    # Make 'alice' a real member so she clears the gate
    client.post("/api/agents/add", json={"actor_id": "admin", "target_agent_id": "alice"})

    with patch.object(web_mod, "LANDiscovery", _CapturingLAN):
        resp = client.post(
            "/api/agents/lan_discover",
            json={"actor_id": "alice", "timeout_seconds": 0.5},
        )
    assert resp.status_code == 200
    assert captured_agent == ["alice"]


# ===== rate limit =====


def test_R5_rate_limit_fires_after_5_calls(client):
    """5 successive calls -> the 6th must 429."""
    with _stub_lan_discovery():
        for i in range(5):
            resp = client.post(
                "/api/agents/lan_discover",
                json={"actor_id": "admin", "timeout_seconds": 0.5},
            )
            assert resp.status_code == 200, f"call {i+1} unexpectedly denied"
        resp = client.post(
            "/api/agents/lan_discover",
            json={"actor_id": "admin", "timeout_seconds": 0.5},
        )
    assert resp.status_code == 429
    assert "retry after" in resp.json()["detail"]


def test_R5_rate_limit_is_per_actor(client):
    """Alice burning her 5-call quota should NOT stop Bob's first call."""
    client.post("/api/agents/add", json={"actor_id": "admin", "target_agent_id": "alice"})
    client.post("/api/agents/add", json={"actor_id": "admin", "target_agent_id": "bob"})
    with _stub_lan_discovery():
        # Alice burns her quota
        for _ in range(5):
            client.post(
                "/api/agents/lan_discover",
                json={"actor_id": "alice", "timeout_seconds": 0.5},
            )
        # Bob's first call should still go through
        resp = client.post(
            "/api/agents/lan_discover",
            json={"actor_id": "bob", "timeout_seconds": 0.5},
        )
    assert resp.status_code == 200
