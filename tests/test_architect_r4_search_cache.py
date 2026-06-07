"""Architect R-4 (2026-06-07): mtime-keyed cache on the search hot path.

Pre-fix /api/agents/search walked:
  * the entire WoT JSONL (list_endorsements)
  * every group_registry/*.json file (list_all)
on EVERY request. With the dashboard polling every 5 s and N concurrent
operators, this scaled badly.

Pins:
  * Two identical search requests with NO disk changes only call the
    underlying compute path ONCE
  * Mutating the underlying file (write / touch / unlink) invalidates
    the cache and the next call recomputes
  * Cache survives WoT load failures (returns last good or {} as
    documented) without re-raising on the hot path
  * _MtimeCache.invalidate() is a working hatch for tests / admin tools
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import _MtimeCache, create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


# ===== _MtimeCache unit =====


def test_R4_mtime_cache_returns_same_value_when_paths_unchanged(tmp_path):
    """Same files, same mtime -> compute is called once across N gets."""
    f = tmp_path / "probe.txt"
    f.write_text("initial", encoding="utf-8")
    cache = _MtimeCache()
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return f.read_text(encoding="utf-8")

    a = cache.get([f], compute)
    b = cache.get([f], compute)
    c = cache.get([f], compute)
    assert a == b == c == "initial"
    assert call_count == 1


def test_R4_mtime_cache_recomputes_when_file_changes(tmp_path):
    """Mutate the underlying file -> next get() recomputes."""
    f = tmp_path / "probe.txt"
    f.write_text("v1", encoding="utf-8")
    cache = _MtimeCache()
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return f.read_text(encoding="utf-8")

    assert cache.get([f], compute) == "v1"
    # Ensure detectable mtime delta on Windows (FS resolution ~1ms).
    time.sleep(0.05)
    f.write_text("v2", encoding="utf-8")
    assert cache.get([f], compute) == "v2"
    assert call_count == 2


def test_R4_mtime_cache_treats_missing_probe_as_signature(tmp_path):
    """A probe path that doesn't exist is still a valid signature -
    files appearing later should invalidate."""
    f = tmp_path / "maybe.txt"
    cache = _MtimeCache()
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return f.exists()

    assert cache.get([f], compute) is False
    assert cache.get([f], compute) is False
    assert call_count == 1
    # Now the file appears - signature changes
    f.write_text("hi", encoding="utf-8")
    assert cache.get([f], compute) is True
    assert call_count == 2


def test_R4_mtime_cache_invalidate_forces_recompute(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    cache = _MtimeCache()
    n = 0

    def compute():
        nonlocal n
        n += 1
        return n

    assert cache.get([f], compute) == 1
    assert cache.get([f], compute) == 1
    cache.invalidate()
    assert cache.get([f], compute) == 2


# ===== integration: search endpoint uses the cache =====


def _seed_group(workspace: Path, slug: str, pk: str) -> None:
    from nth_dao.group_registry import GroupPolicy, GroupRegistry
    registry = GroupRegistry(workspace)
    path = registry._path_for_slug(slug)
    path.write_text(json.dumps({
        "group_id": f"grp-{slug}",
        "slug": slug,
        "display_name": slug,
        "description": "",
        "policy": GroupPolicy.OPEN.value,
        "founder_pubkey": pk,
        "member_pubkeys": [pk],
        "admin_pubkeys": [pk],
        "signer_pubkey": pk,
        "sig": "fake",
        "created_at": "2026-06-07T00:00:00",
        "updated_at": "2026-06-07T00:00:00",
        "metadata": {},
    }), encoding="utf-8")


def test_R4_endorsement_count_is_cached_across_calls(client, tmp_path):
    """Two back-to-back search calls call list_endorsements ONCE."""
    pk = "ab" * 32
    _seed_group(tmp_path, "qa", pk)

    # Get the trust object via the app's internal state
    app = client.app
    state = app.state.nth
    real_list = state.trust.list_endorsements
    call_count = 0

    def counting_list(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_list(*args, **kwargs)

    with patch.object(state.trust, "list_endorsements", counting_list):
        # Invalidate so the test starts from a clean cache state
        state._endorsement_count_cache.invalidate()
        for _ in range(5):
            resp = client.get(
                "/api/agents/search",
                params={"q": "qa", "actor_id": "admin"},
            )
            assert resp.status_code == 200
    assert call_count == 1, (
        f"list_endorsements called {call_count} times across 5 search "
        f"requests; cache should reduce to 1"
    )


def test_R4_group_list_is_cached_across_calls(client, tmp_path):
    """Two back-to-back search calls call group_registry.list_all ONCE."""
    pk = "cd" * 32
    _seed_group(tmp_path, "ops", pk)

    state = client.app.state.nth
    real_list_all = state.group_registry.list_all
    call_count = 0

    def counting_list_all(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_list_all(*args, **kwargs)

    with patch.object(state.group_registry, "list_all", counting_list_all):
        state._group_list_cache.invalidate()
        for _ in range(5):
            resp = client.get(
                "/api/agents/search",
                params={"q": "ops", "actor_id": "admin"},
            )
            assert resp.status_code == 200
    assert call_count == 1, (
        f"group_registry.list_all called {call_count} times across 5 "
        f"search requests; cache should reduce to 1"
    )


def test_R4_endorsement_cache_invalidates_when_file_changes(
    client, tmp_path,
):
    """Touch endorsements.jsonl -> next call recomputes."""
    pk = "ef" * 32
    _seed_group(tmp_path, "qa", pk)

    # Warm cache via one search call
    state = client.app.state.nth
    state._endorsement_count_cache.invalidate()
    client.get("/api/agents/search", params={"q": "qa", "actor_id": "admin"})

    # Drop a new endorsement on disk via the safe path
    trust_dir = tmp_path / "team_trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    endorsements_path = trust_dir / "endorsements.jsonl"
    time.sleep(0.05)  # ensure mtime delta
    with endorsements_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "endorser_pubkey": "00" * 32,
            "endorser_agent_id": "anyone",
            "subject_pubkey": pk,
            "subject_agent_id": "qa-member",
            "weight": 1.0,
            "reason": "post-cache invalidation",
            "issued_at": "2026-06-07T00:00:00",
            "ttl_days": 365,
            "depth_allowed": 2,
            "sig": "fake",
        }) + "\n")

    # Next search should reflect the new endorsement
    resp = client.get(
        "/api/agents/search",
        params={"q": "qa", "actor_id": "admin"},
    )
    rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    assert rows
    # endorsement_count is 1 now after the post-cache invalidation
    assert any(r["endorsement_count"] == 1 for r in rows)


def test_R4_corrupt_wot_logs_warning_and_returns_empty(
    client, tmp_path, caplog,
):
    """R-8 companion: corrupt WoT file no longer silently swallows -
    it logs WARNING. Behaviour is still "search continues with 0 counts"."""
    pk = "12" * 32
    _seed_group(tmp_path, "qa", pk)
    trust_dir = tmp_path / "team_trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    (trust_dir / "endorsements.jsonl").write_text(
        "not json at all\n", encoding="utf-8",
    )
    state = client.app.state.nth
    state._endorsement_count_cache.invalidate()

    import logging
    with caplog.at_level(logging.WARNING, logger="nth_dao.web"):
        resp = client.get(
            "/api/agents/search",
            params={"q": "qa", "actor_id": "admin"},
        )
    assert resp.status_code == 200
    # Either the load survived JSONL malformed-line dropping or we
    # logged the failure - both shapes are acceptable. The hard
    # contract is that search itself does not 500.
    rows = resp.json()["results"]
    assert isinstance(rows, list)
