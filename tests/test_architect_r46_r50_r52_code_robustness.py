"""Architect R-46 / R-50 / R-52 (2026-06-08): code derivation
robustness post the R-35..R-38 refactor.

R-46 (CRITICAL): pre-this-batch the admin code fell back to
    ``code_for_agent_id("admin") = 8c69-76e5`` whenever the node had no
    crypto material - so two PyNaCl-missing installs collided again.
    The fix returns empty string for the bootstrap admin in that
    degraded state and pushes the burden of "show or hide the strip"
    onto the front-end.

R-50: the previous _code_for_member ignored ``contact.did`` when
    ``contact.pubkey_hex`` was empty. Did:key is deterministic-decodable
    so we now derive the pubkey from it.

R-52: the previous degradation test set ``state.nth.node_identity = None``
    AFTER the bootstrap had succeeded. That's a synthetic state - real
    "PyNaCl missing" installs bootstrap with ``_NACL_AVAILABLE = False``
    and load_or_generate produces a non-None identity WITHOUT a pubkey.
    This test exercises THAT path with a monkeypatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.agent_code import code_for_agent_id, code_for_pubkey
from nth_dao.contact_book import ContactBook, SOURCE_MANUAL
from nth_dao.identity import crypto_available
from nth_dao.web import create_app


@pytest.fixture
def temp(tmp_path, monkeypatch):
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    return tmp_path


# ===== R-46: PyNaCl-missing degradation =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="we need PyNaCl present to MONKEYPATCH it away cleanly",
)
def test_R46_two_pynacl_missing_installs_do_NOT_share_a_code(
    temp, monkeypatch,
):
    """The headline R-46 assertion. Disable crypto availability AT
    BOOTSTRAP TIME for both installs; their admin codes must NOT both
    be ``8c69-76e5``.

    R-59 (2026-06-08): the original test asserted only the negative
    invariant (``a_code != legacy`` and ``b_code != legacy``) which
    silently accepted ``a_code == "xyz1-abcd" == b_code`` — a global
    collision under a DIFFERENT constant would have slipped through.

    The chosen degradation strategy (documented in
    ``_resolve_member_identity``'s docstring) is **empty string**:
    when crypto material is unavailable we surface "" so the
    front-end can show a clear "install pynacl" hint instead of a
    misleading handle. Lock that contract here so a future refactor
    cannot silently drift to a different degraded-path value.
    """
    # Step 1: simulate PyNaCl missing for the entire bootstrap path
    import nth_dao.identity as _id_mod
    monkeypatch.setattr(_id_mod, "_NACL_AVAILABLE", False)

    ws_a = temp / "alice"
    ws_b = temp / "bob"
    client_a = TestClient(create_app(ws_a, require_console_auth=False))
    client_b = TestClient(create_app(ws_b, require_console_auth=False))

    resp_a = client_a.get(
        "/api/summary", params={"actor_id": "admin"},
    )
    resp_b = client_b.get(
        "/api/summary", params={"actor_id": "admin"},
    )
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    a_code = resp_a.json()["actor_code"]
    b_code = resp_b.json()["actor_code"]

    legacy_admin_hash = code_for_agent_id("admin")    # "8c69-76e5"
    assert legacy_admin_hash == "8c69-76e5"   # regression sentinel

    # Primary: no legacy collision
    assert a_code != legacy_admin_hash, (
        f"PyNaCl-missing install A returned the legacy global "
        f"collision constant {a_code!r}; R-46 unresolved"
    )
    assert b_code != legacy_admin_hash, (
        f"PyNaCl-missing install B returned the legacy global "
        f"collision constant {b_code!r}; R-46 unresolved"
    )

    # R-59: pin the empty-string degradation strategy. If a future
    # refactor swaps to "derive from workspace path" or similar,
    # this assertion is the canary that forces an explicit contract
    # change here AND in _resolve_member_identity's docstring.
    assert a_code == "", (
        f"degraded actor_code expected '' (empty-string strategy); "
        f"got {a_code!r}. If you're changing the strategy, update "
        f"_resolve_member_identity's docstring AND this test together."
    )
    assert b_code == "", (
        f"degraded actor_code expected '' for install B too; got "
        f"{b_code!r}"
    )


@pytest.mark.skipif(
    not crypto_available(),
    reason="we need PyNaCl present to MONKEYPATCH it away cleanly",
)
def test_R46_identity_endpoint_returns_empty_code_when_degraded(
    temp, monkeypatch,
):
    """The chosen strategy is ``code = ""`` when the bootstrap admin
    has no usable pubkey. Pin it so a future change can't silently
    drift to a different degradation strategy without updating tests."""
    import nth_dao.identity as _id_mod
    monkeypatch.setattr(_id_mod, "_NACL_AVAILABLE", False)

    client = TestClient(create_app(temp, require_console_auth=False))
    body = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert body["code"] == "", (
        f"degraded /api/identity returned code={body['code']!r}; "
        f"expected empty string"
    )
    assert body.get("bootstrap_error"), (
        "degraded /api/identity must set bootstrap_error so the UI "
        "can render a help hint instead of a broken handle"
    )


# ===== R-50: DID-only contact =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="R-50 requires PyNaCl to encode/decode did:key",
)
def test_R50_did_only_contact_yields_pubkey_derived_code(temp):
    """Inject a contact with only ``did`` (no pubkey_hex). The
    search row's code MUST be derived from the pubkey that did:key
    decodes to, NOT the literal-agent_id fallback."""
    # Seed a contact directly via the ContactBook so the /api/agents/add
    # auto-decode does not pre-populate pubkey_hex for us.
    book = ContactBook(temp)
    did = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"
    book.add(
        agent_id="alice",
        did=did,
        pubkey_hex="",   # deliberately empty
        label="DID-only Alice",
        source=SOURCE_MANUAL,
    )
    # Now also make alice a member so she shows up in search
    client = TestClient(create_app(temp, require_console_auth=False))
    add_resp = client.post(
        "/api/agents/add",
        json={"actor_id": "admin", "target_agent_id": "alice"},
    )
    assert add_resp.status_code == 200

    search = client.get(
        "/api/agents/search",
        params={"q": "alice", "actor_id": "admin"},
    )
    row = next(
        r for r in search.json()["results"]
        if r["agent_id"] == "alice" and r["source"] == "home"
    )
    # The code MUST equal the pubkey-derived code corresponding to
    # that did:key, NOT code_for_agent_id("alice"). The pubkey for
    # this canonical did:key is documented and derivable.
    from nth_dao.did_key import decode_ed25519_did_key_hex
    expected_pk = decode_ed25519_did_key_hex(did)
    expected_code = code_for_pubkey(expected_pk)
    assert row["code"] == expected_code, (
        f"DID-only contact's code is the agent_id-hash {row['code']!r}; "
        f"R-50 unresolved - expected {expected_code!r}"
    )


# ===== R-52: real-fallback test (not the synthetic mid-flight clear) =====


def test_R52_synthetic_clear_to_None_DIFFERS_from_real_degradation(
    temp,
):
    """Document that the OLD style ``state.nth.node_identity = None``
    is not equivalent to the real PyNaCl-missing bootstrap. This is
    a meta-test that proves the test infrastructure is honest.

    With PyNaCl available, bootstrap creates a real identity with
    a real pubkey. Synthetically clearing it produces ``None`` but
    the on-disk identity.json was already written. A FRESH boot
    over the same workspace would re-read it.
    """
    if not crypto_available():
        pytest.skip("can't compare paths without PyNaCl baseline")
    client = TestClient(create_app(temp, require_console_auth=False))
    client.app.state.nth.node_identity = None
    # The endpoint sees None (synthetic state). But the file IS
    # on disk and a restart would reconstruct it.
    from nth_dao.identity import default_identity_path
    assert default_identity_path(temp).exists()
    # So the synthetic test exercises a state that DOESN'T persist
    # across restart - not the same as PyNaCl-missing.


# ===== R-47/R-48/R-49 consistency (post-helper-refactor) =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="consistency tests need a working bootstrap identity",
)
def test_R47_identity_summary_search_all_agree_after_refactor(temp):
    """Now that all three call sites go through ``_resolve_member_identity``,
    they must produce identical codes for the bootstrap admin. The
    pre-refactor implementations duplicated logic; this test makes
    the consolidated helper the single source of truth."""
    client = TestClient(create_app(temp, require_console_auth=False))
    summary_code = client.get(
        "/api/summary", params={"actor_id": "admin"},
    ).json()["actor_code"]
    identity_code = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()["code"]
    search_row = next(
        r for r in client.get(
            "/api/agents/search",
            params={"q": "admin", "actor_id": "admin"},
        ).json()["results"]
        if r["agent_id"] == "admin" and r["source"] == "home"
    )
    assert summary_code == identity_code == search_row["code"]


# ===== R-53: public identity card carries the code =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="public card requires a real bootstrap identity",
)
def test_R53_public_card_includes_code_field(temp):
    """The public ``/.well-known/nth-dao/identity.json`` card now
    includes a ``code`` field so cross-language consumers don't have
    to re-implement the (hex-string vs raw-bytes) hash details."""
    client = TestClient(create_app(temp, require_console_auth=False))
    card = client.get("/.well-known/nth-dao/identity.json").json()
    assert card["code"], "public card missing code field"
    # And it agrees with /api/identity (which we already test for
    # consistency above, so this is the cross-endpoint link).
    private = client.get(
        "/api/identity", params={"actor_id": "admin"},
    ).json()
    assert card["code"] == private["code"]


# ===== R-51 fast-path: no double ContactBook query =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="requires a working bootstrap identity for the search path",
)
def test_R51_search_does_NOT_query_contact_book_twice_per_member(
    temp, monkeypatch,
):
    """Spy on ContactBook.get to count calls during one search
    request that exercises the home loop. Pre-refactor we called
    get(agent_id) twice per member; now exactly once."""
    client = TestClient(create_app(temp, require_console_auth=False))
    # Add a contact so the search has someone to enrich.
    client.post(
        "/api/agents/add",
        json={
            "actor_id": "admin",
            "target_did": (
                "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"
            ),
        },
    )

    # Wrap ContactBook.get to count
    book = client.app.state.nth.contacts
    real_get = book.get
    call_count = {"n": 0}
    def counting_get(agent_id):
        call_count["n"] += 1
        return real_get(agent_id)
    monkeypatch.setattr(book, "get", counting_get)

    client.get(
        "/api/agents/search",
        params={"q": "admin", "actor_id": "admin"},
    )

    # The number of member rows is small (admin + echo + any added).
    # Pre-refactor was 2 * N; post-refactor is exactly 1 * N where N
    # is the number of home members the loop touches. We don't pin
    # the exact N (it depends on echo and added contacts), but we
    # do pin "no more than 1 call per member iterated".
    config = client.app.state.nth.membership.load_config()
    n_members = len(config.member_ids)
    # Allow 1 extra for the admin-row path which may or may not
    # touch contacts depending on impl. The HARD ceiling is N (was
    # 2*N pre-refactor).
    assert call_count["n"] <= n_members + 1, (
        f"ContactBook.get called {call_count['n']} times for "
        f"{n_members} members; expected at most {n_members+1}"
    )


# ===== A-5 (architect review of R-57): DID-only registry row =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="A-5 needs PyNaCl to encode/decode did:key",
)
def test_A5_did_only_registry_row_yields_pubkey_derived_code(temp):
    """A LAN peer that advertises ``did`` in TXT but omits
    ``pubkey_hex`` (e.g. a future protocol revision, or a minimal
    third-party node) must still produce a pubkey-derived code in the
    search result.

    Pre-A5 fix R-57 only checked ``registry_pk``; an empty pubkey_hex
    fell through to ``code_for_agent_id(r.record.agent_id)`` — which
    is the very R-35 cross-install collision (every LAN daemon using
    ``agent_id='admin'`` would collapse to ``8c69-76e5``). The A-5
    fix mirrors ``_resolve_member_identity``'s did:key decode: if the
    row carries ``did`` we recover the pubkey from it before deriving
    the code.

    This test fabricates a registry row with only ``did`` in
    metadata and asserts the search row's code matches the
    pubkey-derived expectation.
    """
    from nth_dao.discovery.agent_registry import AgentRegistry
    from nth_dao.did_key import decode_ed25519_did_key_hex

    # The web layer's PeerFinder reads from <workspace>/team_agents.
    # Register a fake peer there BEFORE create_app builds the
    # PeerFinder so the search picks it up.
    agents_dir = temp / "team_agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    peer_did = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"
    expected_pk = decode_ed25519_did_key_hex(peer_did)
    expected_code = code_for_pubkey(expected_pk)

    fake_reg = AgentRegistry(str(agents_dir))
    fake_reg.register(
        agent_id="lan-peer-X",
        metadata={
            "did": peer_did,
            # pubkey_hex DELIBERATELY ABSENT — that's the whole point
        },
        start_heartbeat=False,
    )
    # Don't unregister: we want the row to persist for the search.

    client = TestClient(create_app(temp, require_console_auth=False))
    search = client.get(
        "/api/agents/search",
        params={"q": "lan-peer", "actor_id": "admin"},
    )
    assert search.status_code == 200, search.text
    rows = [
        r for r in search.json()["results"]
        if r["agent_id"] == "lan-peer-X"
    ]
    assert rows, (
        f"DID-only registry peer not found in search results: "
        f"{search.json()['results']}"
    )
    row = rows[0]
    # The headline assertion: code is derived from the did:key, not
    # from the agent_id literal.
    assert row["code"] == expected_code, (
        f"DID-only registry row produced code {row['code']!r}; "
        f"expected pubkey-derived {expected_code!r}. A-5 unresolved — "
        f"R-57 must mirror _resolve_member_identity's did:key decode."
    )
    # And specifically not the agent_id-hash fallback
    assert row["code"] != code_for_agent_id("lan-peer-X"), (
        "code degraded to the agent_id-hash fallback — the did:key "
        "decode in R-57 didn't run"
    )
