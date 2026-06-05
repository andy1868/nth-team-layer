"""P2: replay protection via timestamp TTL + persistent nonce ledger.

The original in-memory dedup cache (_seen) only protected against
within-process retries. A signed request could be:
  - replayed days later (no TTL)
  - replayed after process restart (cache reset)
  - replayed after enough other traffic evicted it (LRU)

Production cross-agent dispatch MUST defend all three. P2 introduces:
  1. TTL check against now - ttl < timestamp < now + skew
  2. Persistent nonce ledger keyed on (from_agent, request_id), with
     automatic sweep of stale entries past ttl+skew
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nth_dao.action_routing import (
    ActionRequest,
    ActionRouter,
    ActionStatus,
)
from nth_dao.identity import AgentIdentity, crypto_available


@pytest.fixture
def alice() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="alice")


@pytest.fixture
def bob() -> AgentIdentity:
    if not crypto_available():
        pytest.skip("PyNaCl required")
    return AgentIdentity.generate(label="bob")


def _build_prod_router(tmp_path: Path, alice: AgentIdentity, bob: AgentIdentity,
                      **overrides) -> ActionRouter:
    defaults = dict(
        agent_id="alice",
        identity=alice,
        pubkey_lookup=lambda aid: bob.pubkey_hex if aid == "bob" else None,
        workspace=tmp_path,
    )
    defaults.update(overrides)
    return ActionRouter(**defaults)   # type: ignore[arg-type]


def _signed(bob: AgentIdentity, *, request_id="r1", timestamp=None,
            to_agent="alice") -> ActionRequest:
    req = ActionRequest(
        request_id=request_id,
        action_type="ping",
        from_agent="bob",
        to_agent=to_agent,
    )
    if timestamp is not None:
        req.timestamp = timestamp
    req.sig = bob.sign_json(req.signable_dict())
    return req


# ===== TTL check =====


def test_P2_TTL_accepts_fresh_request(tmp_path: Path, alice, bob):
    router = _build_prod_router(tmp_path, alice, bob)
    router.register("ping", lambda r: "pong")
    req = _signed(bob)   # timestamp = now() at construction
    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


def test_P2_TTL_rejects_old_request(tmp_path: Path, alice, bob):
    router = _build_prod_router(
        tmp_path, alice, bob,
        request_ttl_seconds=10.0,
    )
    router.register("ping", lambda r: "pong")
    # Timestamp 5 minutes ago — way past TTL of 10s
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    req = _signed(bob, timestamp=stale)
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert "expired" in resp.error
    assert "age=" in resp.error
    assert resp.sig == ""   # H-6: reject responses not signed


def test_P2_TTL_rejects_future_request_beyond_skew(tmp_path: Path, alice, bob):
    router = _build_prod_router(
        tmp_path, alice, bob,
        request_ttl_seconds=60.0,
        clock_skew_seconds=10.0,
    )
    router.register("ping", lambda r: "pong")
    future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    req = _signed(bob, timestamp=future)
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert "from the future" in resp.error


def test_P2_TTL_naive_timestamp_rejected(tmp_path: Path, alice, bob):
    router = _build_prod_router(tmp_path, alice, bob)
    router.register("ping", lambda r: "pong")
    # Naive local time — exactly what main was emitting before C-8
    naive = datetime.now().isoformat()   # no tzinfo
    req = _signed(bob, timestamp=naive)
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert "naive timestamp" in resp.error


def test_P2_TTL_malformed_timestamp_rejected(tmp_path: Path, alice, bob):
    router = _build_prod_router(tmp_path, alice, bob)
    router.register("ping", lambda r: "pong")
    req = _signed(bob, timestamp="not-a-timestamp")
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert "malformed timestamp" in resp.error


def test_P2_TTL_zero_seconds_disables_check(tmp_path: Path, alice, bob):
    """ttl=0 is an explicit opt-out (stateless smoke tests)."""
    router = _build_prod_router(
        tmp_path, alice, bob, request_ttl_seconds=0,
    )
    router.register("ping", lambda r: "pong")
    stale = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    req = _signed(bob, timestamp=stale)
    resp = router.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


# ===== persistent nonce ledger =====


def test_P2_NONCE_blocks_replay_after_restart(tmp_path: Path, alice, bob):
    """The whole point: simulate process restart, the same signed
    request must not be accepted a second time."""
    router1 = _build_prod_router(tmp_path, alice, bob)
    router1.register("ping", lambda r: "pong")
    req = _signed(bob)
    resp1 = router1.handle(req)
    assert resp1.status == ActionStatus.COMPLETED.value

    # Simulate restart — fresh router instance over the SAME workspace.
    # In-memory _seen is empty; only the persistent nonce ledger can
    # protect us.
    router2 = _build_prod_router(tmp_path, alice, bob)
    router2.register("ping", lambda r: "pong")
    resp2 = router2.handle(req)
    assert resp2.status == ActionStatus.REJECTED.value
    assert "replay" in resp2.error


def test_P2_NONCE_blocks_replay_after_cache_eviction(
    tmp_path: Path, alice, bob,
):
    """The in-memory cache evicts under load (LRU at max_dedup_entries).
    The nonce ledger must hold even when _seen has forgotten the entry."""
    router = _build_prod_router(
        tmp_path, alice, bob, max_dedup_entries=2,
    )
    router.register("ping", lambda r: r.request_id)
    original = _signed(bob, request_id="original")
    assert router.handle(original).status == ActionStatus.COMPLETED.value
    # Flood with new requests to evict 'original' from _seen
    for i in range(5):
        router.handle(_signed(bob, request_id=f"flood{i}"))
    # _seen has long forgotten 'original'
    assert ("bob", "original") not in router._seen
    # But the nonce ledger remembers
    replay = router.handle(original)
    assert replay.status == ActionStatus.REJECTED.value


def test_P2_NONCE_disabled_in_dev_mode_by_default(tmp_path: Path):
    """Dev mode skips the ledger so smoke tests don't churn disk."""
    router = ActionRouter(
        agent_id="alice", workspace=tmp_path,
        allow_unsigned_dev=True,
    )
    assert router._enable_nonce_ledger is False


def test_P2_NONCE_can_be_explicitly_disabled_in_prod(tmp_path: Path, alice, bob):
    router = _build_prod_router(
        tmp_path, alice, bob, enable_nonce_ledger=False,
    )
    router.register("ping", lambda r: "pong")
    req = _signed(bob)
    router.handle(req)
    # Restart, same request — would normally fail; opted out, so passes.
    router2 = _build_prod_router(
        tmp_path, alice, bob, enable_nonce_ledger=False,
    )
    router2.register("ping", lambda r: "pong")
    resp = router2.handle(req)
    assert resp.status == ActionStatus.COMPLETED.value


def test_P2_NONCE_rejected_requests_are_not_recorded(tmp_path: Path, alice, bob):
    """A request that was rejected (e.g. unknown handler -> FAILED, or
    misdirected -> REJECTED) must not poison the ledger; the same
    request_id can legitimately be retried with a fix."""
    router = _build_prod_router(tmp_path, alice, bob)
    # No handler registered for 'ping' -> FAILED (the handler doesn't exist)
    # But wait, FAILED IS truly executed (handler lookup failed but request
    # went through the verify pipeline). Re-test rejection path instead:
    # misdirected request -> REJECTED -> must not poison ledger.
    misdirected = _signed(bob, to_agent="carol")
    resp1 = router.handle(misdirected)
    assert resp1.status == ActionStatus.REJECTED.value
    # The same (from_agent, request_id) but now correctly addressed
    correct = _signed(bob, to_agent="alice")
    router.register("ping", lambda r: "pong")
    resp2 = router.handle(correct)
    assert resp2.status == ActionStatus.COMPLETED.value


def test_P2_NONCE_sweeps_stale_entries(tmp_path: Path, alice, bob):
    """Old ledger entries past ttl+skew should be swept on load."""
    router = _build_prod_router(
        tmp_path, alice, bob,
        request_ttl_seconds=1.0,
        clock_skew_seconds=0.0,
    )
    router.register("ping", lambda r: "pong")
    # Manually plant an ancient entry
    ancient_ts = datetime.now(timezone.utc).timestamp() - 3600
    router._nonce_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    router._nonce_ledger_path.write_text(
        json.dumps({"old|x": ancient_ts}), encoding="utf-8",
    )
    # _load_nonce_ledger sweeps anything older than ttl+skew
    loaded = router._load_nonce_ledger()
    assert "old|x" not in loaded


def test_P2_NONCE_failed_handler_still_records_nonce(tmp_path: Path, alice, bob):
    """A request that REACHED the handler but the handler errored
    (FAILED status) HAS executed - retrying with the same request_id
    must not bypass the dedup. Only true REJECTED responses don't
    record."""
    router = _build_prod_router(tmp_path, alice, bob)
    def boom(_r):
        raise RuntimeError("intentional")
    router.register("boom", boom)
    req = ActionRequest(
        request_id="r1", action_type="boom",
        from_agent="bob", to_agent="alice",
    )
    req.sig = bob.sign_json(req.signable_dict())

    resp1 = router.handle(req)
    assert resp1.status == ActionStatus.FAILED.value

    # Restart with the same workspace -> the failed nonce was recorded
    router2 = _build_prod_router(tmp_path, alice, bob)
    router2.register("boom", boom)
    resp2 = router2.handle(req)
    assert resp2.status == ActionStatus.REJECTED.value   # replay block
