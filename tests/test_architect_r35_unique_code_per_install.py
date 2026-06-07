"""Architect R-35 / R-36 / R-37 / R-38 (2026-06-08): per-install
visible code uniqueness.

Pre-fix every endpoint that returned a "code" field hashed the LITERAL
string ``"admin"`` via ``code_for_agent_id``. Result: every NTH DAO
install displayed ``8c69-76e5`` as "Your code" - so two LAN peers
saw their own dashboards advertising the SAME visible identifier,
and the by_code reverse lookup always found the local admin first
regardless of who the operator was actually trying to resolve.

The fix derives the bootstrap admin's code from the workspace's
Ed25519 pubkey (``code_for_pubkey(node_identity.pubkey_hex)``). Two
workspaces with distinct keypairs now produce distinct codes.
Contact-book lookups for added agents follow the same rule when the
contact carries a pubkey.

Pins:
  * /api/summary actor_code is per-install unique
  * /api/identity code is per-install unique AND equals the summary
    actor_code (consistency across endpoints)
  * /api/agents/search admin home row has per-install unique code
  * /api/agents/by_code reverse lookup finds the right admin even
    when the local admin shares the legacy "admin" agent_id
  * Restart-stable: code computed once doesn't drift after restart
  * Degraded fallback when node_identity is None (no PyNaCl):
    the legacy agent_id-derived code is still returned with
    bootstrap_error set so consumers can detect the degraded state
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.agent_code import code_for_agent_id, code_for_pubkey
from nth_dao.identity import crypto_available
from nth_dao.web import create_app


pytestmark = pytest.mark.skipif(
    not crypto_available(),
    reason="per-install code uniqueness requires PyNaCl",
)


@pytest.fixture
def alice(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    return TestClient(
        create_app(tmp_path / "alice", require_console_auth=False),
    )


@pytest.fixture
def bob(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    return TestClient(
        create_app(tmp_path / "bob", require_console_auth=False),
    )


# ===== /api/summary: two installs => two codes =====


def test_R35_two_installs_get_distinct_actor_codes(alice, bob):
    """The headline assertion. If this fails, ``Your code`` is
    globally identical and the dashboard lies to the operator."""
    a_code = alice.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    b_code = bob.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    assert a_code, "alice summary returned empty actor_code"
    assert b_code, "bob summary returned empty actor_code"
    assert a_code != b_code, (
        f"two installs produced the SAME visible code {a_code!r}; "
        "see R-35 - actor_code is hashed from the literal 'admin' "
        "string instead of the node pubkey"
    )


def test_R35_actor_code_no_longer_matches_the_admin_string_hash(alice):
    """Specifically: it must NOT be the cryptographically-trivial
    ``code_for_agent_id('admin')`` constant (8c69-76e5). That string
    was the regression sentinel."""
    a_code = alice.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    admin_literal = code_for_agent_id("admin")
    # Verify the failure mode IS what we expect to defeat
    assert admin_literal == "8c69-76e5"
    assert a_code != admin_literal, (
        f"actor_code {a_code!r} still equals the literal-admin hash; "
        f"R-35 is unresolved"
    )


def test_R35_actor_code_equals_pubkey_derived_code(alice):
    """Documented contract: actor_code is the SHA-256 truncate-8 of
    the pubkey hex, so a remote consumer who has the pubkey can
    independently compute the expected code without an extra fetch."""
    summary = alice.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()
    identity = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    expected = code_for_pubkey(identity["pubkey_hex"])
    assert summary["actor_code"] == expected


# ===== /api/identity: same code (consistency across endpoints) =====


def test_R36_identity_code_is_per_install_unique(alice, bob):
    a = alice.get("/api/identity", params={"actor_id": "admin"}).json()
    b = bob.get("/api/identity", params={"actor_id": "admin"}).json()
    assert a["code"] != b["code"]


def test_R36_identity_code_consistent_with_summary_code(alice):
    """Same identifier, two endpoints. If they drift the front-end's
    'this is Bob's code, share it with Alice' workflow breaks."""
    sum_code = alice.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    ident_code = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    assert sum_code == ident_code


def test_R36_state_actor_code_consistent_with_identity_code(alice):
    """The actor block powers the chat shell. It must not regress to
    the literal-admin hash while summary/identity use pubkey codes."""
    state = alice.get(
        "/api/state", params={"agent_id": "admin"},
    ).json()
    ident = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert state["actor"]["code"] == ident["code"]
    assert state["actor"]["code"] != code_for_agent_id("admin")


def test_R36_members_admin_code_consistent_with_identity_code(alice):
    """The members panel and header must show the same handle for the
    same bootstrap identity."""
    state = alice.get(
        "/api/state", params={"agent_id": "admin"},
    ).json()
    ident = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    admin = next(m for m in state["members"] if m["agent_id"] == "admin")
    assert admin["code"] == ident["code"]
    assert admin["did"] == ident["did"]


# ===== search home row =====


def test_R37_search_admin_home_row_has_per_install_unique_code(
    alice, bob,
):
    a_row = next(
        r for r in alice.get(
            "/api/agents/search",
            params={"q": "admin", "actor_id": "admin"},
        ).json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    )
    b_row = next(
        r for r in bob.get(
            "/api/agents/search",
            params={"q": "admin", "actor_id": "admin"},
        ).json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    )
    assert a_row["code"] != b_row["code"], (
        f"both installs' admin home rows show code {a_row['code']!r}; "
        f"R-37 unresolved"
    )


def test_R37_search_admin_row_code_matches_identity_code(alice):
    row = next(
        r for r in alice.get(
            "/api/agents/search",
            params={"q": "admin", "actor_id": "admin"},
        ).json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    )
    ident = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert row["code"] == ident["code"]


# ===== by_code reverse lookup =====


def test_R38_by_code_resolves_to_correct_admin(alice):
    """Round-trip: ask /api/identity for our code, then ask
    /api/agents/by_code for the agent owning that code - the answer
    must be us. Pre-fix the comparison was against the literal
    "admin" hash, so the lookup found the local admin regardless of
    the actual pubkey behind the code."""
    code = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    found = alice.get(
        f"/api/agents/by_code/{code}",
        params={"actor_id": "admin"},
    ).json()
    assert found["agent_id"] == "admin"
    assert found["source"] == "home"


def test_R38_by_code_does_NOT_resolve_an_arbitrary_legacy_admin_hash(alice):
    """Pasting the LITERAL ``8c69-76e5`` (the broken cross-install
    constant) into by_code must NOT find the local admin - the local
    admin has a unique pubkey-derived code now."""
    legacy_constant = code_for_agent_id("admin")
    resp = alice.get(
        f"/api/agents/by_code/{legacy_constant}",
        params={"actor_id": "admin"},
    )
    # 404 is the right answer: no member in this workspace has that
    # code because it's the cross-install constant nobody uses.
    assert resp.status_code == 404


def test_R38_by_code_returns_pubkey_prefix_for_admin_caller(alice):
    """The by_code response now surfaces pubkey_prefix and (for admin
    caller) the full pubkey_hex so the consumer can verify the
    identity chain - not just trust the agent_id label."""
    code = alice.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    found = alice.get(
        f"/api/agents/by_code/{code}",
        params={"actor_id": "admin"},
    ).json()
    assert len(found.get("pubkey_prefix", "")) == 16
    assert len(found.get("pubkey_hex", "")) == 64


# ===== restart stability =====


def test_R35_code_is_stable_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    a1 = TestClient(create_app(tmp_path, require_console_auth=False))
    code1 = a1.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    a1.close()
    a2 = TestClient(create_app(tmp_path, require_console_auth=False))
    code2 = a2.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    assert code1 == code2


# ===== degraded path: no PyNaCl => legacy code (with warning) =====


def test_R36_no_identity_returns_empty_code_with_bootstrap_error(
    tmp_path, monkeypatch,
):
    """R-46 (2026-06-08): when the bootstrap admin has no crypto
    material, the endpoint returns ``code = ""`` (NOT the legacy
    literal-admin hash which collided globally). The front-end
    treats "" as "code unavailable" and renders a help hint from
    ``bootstrap_error``.

    The PRIOR contract (return ``code_for_agent_id("admin")``) was
    R-46 itself - it reintroduced the very R-35 collision the rest
    of this batch set out to kill.
    """
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    client = TestClient(create_app(tmp_path, require_console_auth=False))
    # Simulate "node_identity went away" by clearing it.
    # (See R-52's dedicated test for the real PyNaCl-missing path.)
    client.app.state.nth.node_identity = None
    body = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert body["code"] == "", (
        f"degraded /api/identity returned code={body['code']!r}; "
        f"R-46 expected empty string"
    )
    assert body["bootstrap_error"]
    # And it MUST NOT be the legacy collision constant
    assert body["code"] != code_for_agent_id("admin")
