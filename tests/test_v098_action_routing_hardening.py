"""Hardening tests for nth_dao.action_routing per Dr. Elena Voss review.

Covers C-1 (thread-safety), C-2 (cache key collision), C-3 (LRU
eviction), C-9 (pubkey_lookup exception), H-3 (concurrent log writes),
H-4 (monotonic clock), H-5 (TypeError swallow), H-6 (response
amplification), M-3 (scope collision), M-8 (HandlerInfo copy).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import pytest

from nth_dao.action_routing import (
    MAX_LOG_TO_AGENT_LEN,
    ActionRequest,
    ActionResponse,
    ActionRouter,
    ActionStatus,
    _dm_scope,
    _truncate,
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


# ─── C-1: handle() is now thread-safe ──────────────────────────────────


def test_C1_handle_idempotency_under_concurrency(tmp_path: Path):
    """100 threads call handle() with the same request_id; the handler
    must run exactly once (the original implementation could run it
    up to 100 times because handle() had no lock)."""
    router = ActionRouter(agent_id="alice", workspace=tmp_path)
    invocations: list = []

    def slow_handler(req):
        invocations.append(req.request_id)
        return {"served": req.request_id}

    router.register("ping", slow_handler)
    req = ActionRequest(
        request_id="single", action_type="ping",
        from_agent="bob", to_agent="alice",
    )

    errors: list = []
    def fire():
        try:
            router.handle(req)
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors
    assert len(invocations) == 1, (
        f"handler ran {len(invocations)} times — idempotency contract broken"
    )


# ─── C-2: cache key includes sender ────────────────────────────────────


def test_C2_two_senders_with_same_request_id_get_distinct_responses(tmp_path: Path):
    """Alice's request_id='r1' and Bob's request_id='r1' must NOT collide."""
    router = ActionRouter(agent_id="server", workspace=tmp_path)
    router.register("echo", lambda req: {"who": req.from_agent})

    req_a = ActionRequest(request_id="r1", action_type="echo",
                          from_agent="alice", to_agent="server")
    req_b = ActionRequest(request_id="r1", action_type="echo",
                          from_agent="bob", to_agent="server")

    resp_a = router.handle(req_a)
    resp_b = router.handle(req_b)

    assert resp_a.result == {"who": "alice"}
    assert resp_b.result == {"who": "bob"}   # Bob did NOT get Alice's cached response


# ─── C-3: LRU eviction order ──────────────────────────────────────────


def test_C3_lru_eviction_keeps_recently_used(tmp_path: Path):
    """Repeatedly hitting an entry must keep it alive while other
    entries are evicted. Original code evicted by insertion order, so
    a hot entry would be evicted regardless of access frequency."""
    router = ActionRouter(agent_id="server", workspace=tmp_path, max_dedup_entries=3)
    router.register("ping", lambda r: r.request_id)

    # Insert 3 entries
    for rid in ("a", "b", "c"):
        router.handle(ActionRequest(
            request_id=rid, action_type="ping",
            from_agent="x", to_agent="server",
        ))
    # Touch "a" so it becomes most-recent
    router.handle(ActionRequest(
        request_id="a", action_type="ping",
        from_agent="x", to_agent="server",
    ))
    # Insert "d" — should evict "b" (least-recently-used), not "a"
    router.handle(ActionRequest(
        request_id="d", action_type="ping",
        from_agent="x", to_agent="server",
    ))
    keys = list(router._seen.keys())
    assert ("x", "a") in keys
    assert ("x", "b") not in keys, "LRU should have evicted 'b', kept the hot 'a'"


# ─── C-9: pubkey_lookup exception is contained ────────────────────────


def test_C9_pubkey_lookup_exception_results_in_clean_reject(tmp_path: Path, alice):
    """A broken pubkey_lookup must not crash handle() — it should
    just reject the request, returning a clean response."""
    def boom(_aid):
        raise RuntimeError("registry is down")

    router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=boom, workspace=tmp_path,
    )
    router.register("ping", lambda r: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="bob", to_agent="alice", sig="ab" * 64,
    )
    # MUST NOT raise
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value


# ─── H-3: concurrent log writes don't corrupt the JSONL ──────────────


def test_H3_concurrent_log_writes_produce_valid_jsonl(tmp_path: Path):
    """50 threads each handling a request → 50 valid JSONL lines, no
    interleaved bytes. Without InterProcessLock on the append, large
    JSON payloads could interleave on POSIX (> PIPE_BUF) or Windows."""
    router = ActionRouter(agent_id="server", workspace=tmp_path)
    router.register("ping", lambda r: {"x": "y" * 5000})   # > PIPE_BUF

    def fire(i: int):
        router.handle(ActionRequest(
            request_id=f"r{i}", action_type="ping",
            from_agent=f"client{i}", to_agent="server",
        ))

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()

    # Every line in each log file must parse as JSON
    for log_name in ("requests_received", "responses_sent"):
        path = router._log_path(log_name)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            json.loads(line)   # MUST NOT raise


# ─── H-4: elapsed_ms uses monotonic clock ─────────────────────────────


def test_H4_elapsed_ms_is_non_negative(tmp_path: Path):
    """Even with clock weirdness, elapsed_ms should be ≥ 0 because we use
    time.monotonic() now."""
    router = ActionRouter(agent_id="server", workspace=tmp_path)
    router.register("instant", lambda r: None)
    req = ActionRequest(request_id="r1", action_type="instant",
                        from_agent="b", to_agent="server")
    resp = router.handle(req)
    assert resp.elapsed_ms >= 0


# ─── H-5: best_match introspection — no TypeError swallow ────────────


def test_H5_unrelated_typeerror_from_best_match_propagates(tmp_path: Path):
    """If best_match itself raises a TypeError for an unrelated reason
    (a bug inside the PeerFinder), we should NOT silently retry with
    different kwargs. The original try/except did exactly that."""
    class BrokenFinder:
        def best_match(self, **kwargs):
            # Modern signature: accepts prefer_status — so introspection
            # commits to that path. Then the unrelated TypeError must
            # propagate, not get retried with prefer_idle.
            raise TypeError("internal bug in PeerFinder")

    router = ActionRouter(agent_id="server", workspace=tmp_path)
    with pytest.raises(TypeError, match="internal bug in PeerFinder"):
        router.dispatch("ping", {}, finder=BrokenFinder())   # type: ignore[arg-type]


# ─── H-6: rejected response not signed, not logged, not cached ────────


def test_H6_rejected_response_is_not_signed(tmp_path: Path, alice):
    """A rejected response must not bear our signature — otherwise the
    attacker gets a free signing oracle."""
    router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=lambda _: None,   # any from_agent → unknown → reject
        workspace=tmp_path,
    )
    router.register("ping", lambda r: "pong")
    req = ActionRequest(
        request_id="r1", action_type="ping",
        from_agent="attacker", to_agent="alice", sig="ff" * 64,
    )
    resp = router.handle(req)
    assert resp.status == ActionStatus.REJECTED.value
    assert resp.sig == "", "rejected response must NOT be signed"


def test_H6_rejected_response_does_not_grow_logs(tmp_path: Path, alice):
    """Rejection path doesn't write to the log files (which the
    attacker would otherwise fill at line-rate)."""
    router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=lambda _: None,
        workspace=tmp_path,
    )
    router.register("ping", lambda r: "pong")
    for i in range(50):
        router.handle(ActionRequest(
            request_id=f"r{i}", action_type="ping",
            from_agent="x" * 10000, to_agent="alice", sig="ff" * 64,
        ))
    # No log files written for the rejected path
    for log_name in ("requests_received", "responses_sent"):
        assert not router._log_path(log_name).exists() or \
               router._log_path(log_name).stat().st_size == 0


def test_H6_rejected_response_to_agent_is_bounded(tmp_path: Path, alice):
    """The rejected response echoes from_agent into to_agent — must be
    truncated so an attacker can't make us emit megabyte-sized strings."""
    router = ActionRouter(
        agent_id="alice", identity=alice,
        pubkey_lookup=lambda _: None,
        workspace=tmp_path,
    )
    huge = "z" * 100_000
    resp = router.handle(ActionRequest(
        request_id="r1", action_type="ping",
        from_agent=huge, to_agent="alice", sig="ff" * 64,
    ))
    assert len(resp.to_agent) <= MAX_LOG_TO_AGENT_LEN + 1   # +1 for ellipsis


# ─── M-3: DM scope is collision-resistant ─────────────────────────────


def test_M3_scope_resistant_to_separator_injection():
    """An agent_id containing the old "--" separator must NOT collide
    with another (a, b) pair."""
    s1 = _dm_scope("a", "b")
    s2 = _dm_scope("a-", "-b")
    s3 = _dm_scope("a--b", "")
    assert s1 != s2
    assert s1 != s3
    assert s2 != s3
    # Order independence: dm_scope(a, b) == dm_scope(b, a)
    assert _dm_scope("a", "b") == _dm_scope("b", "a")


# ─── M-8: HandlerInfo defensive copy ─────────────────────────────────


def test_M8_handler_metadata_is_defensive_copied(tmp_path: Path):
    """If the registrant mutates the dict after register(), the
    registry must not see the mutation."""
    router = ActionRouter(agent_id="x", workspace=tmp_path)
    md = {"timeout": 100}
    router.register("ping", lambda r: None, metadata=md)
    md["timeout"] = 999   # caller mutates after registration
    md["sneak"] = "in"
    info = router.handler_info("ping")
    assert info is not None
    assert info.metadata == {"timeout": 100}   # snapshot, not reference


# ─── _truncate helper ─────────────────────────────────────────────────


def test_truncate_bounds_and_marks():
    assert _truncate("", 10) == ""
    assert _truncate("short", 10) == "short"
    assert _truncate("x" * 1000, 10) == "x" * 10 + "…"
