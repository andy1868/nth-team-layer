"""G-1 (Voss audit): attach() must emit `agent.preflight` to EventBus
regardless of preflight outcome.

The original code had a docstring promising the event would fire but
the actual code path raised BackendUnavailableError before reaching
any emit() call. This file pins the fix:

  * Successful preflight emits ok=True event
  * Failed preflight emits ok=False event AND raises
  * Skipped preflight (skip_preflight=True) emits no event
  * Event payload carries backend_id, ok, detail, duration_ms
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from team_layer.backends.base import AgentBackend, BackendUnavailableError
from team_layer.backends.mock import MockBackend


class _BadBackend(AgentBackend):
    backend_id = "g1_bad"

    @classmethod
    def is_available(cls, **kwargs) -> bool:
        return False

    def start_session(self, config): pass
    def send_turn(self, prompt, system_prompt=""): pass
    def end_session(self): pass


def _read_preflight_events(workspace) -> list:
    """Read all agent.preflight events from the workspace EventBus."""
    from nth_dao.event_bus import EventBus
    bus = EventBus(workspace)
    return [
        e for e in bus.replay(event_types=["agent.preflight"])
    ]


def test_G1_successful_preflight_emits_ok_true_event(tmp_path):
    from nth_dao.attach import attach
    session = attach(
        agent_id="g1-ok-agent",
        backend=MockBackend(),
        workspace=str(tmp_path),
        start_heartbeat=False,
    )
    try:
        events = _read_preflight_events(tmp_path)
        assert len(events) >= 1, "no agent.preflight event was emitted"
        ev = events[-1]
        assert ev.payload["ok"] is True
        assert ev.payload["backend_id"] == "mock"
        assert ev.payload["agent_id"] == "g1-ok-agent"
        assert "duration_ms" in ev.payload
    finally:
        session.detach()


def test_G1_failed_preflight_emits_ok_false_event_AND_raises(tmp_path):
    from nth_dao.attach import attach
    with pytest.raises(BackendUnavailableError, match="preflight failed"):
        attach(
            agent_id="g1-bad-agent",
            backend=_BadBackend(),
            workspace=str(tmp_path),
            start_heartbeat=False,
        )

    # The audit chain must show why the attach was refused, even
    # though the call raised. This is the whole point of G-1.
    events = _read_preflight_events(tmp_path)
    assert len(events) >= 1, (
        "preflight failure left no trace on the audit chain - "
        "the bus is the single source of truth for forensic replay"
    )
    ev = events[-1]
    assert ev.payload["ok"] is False
    assert ev.payload["backend_id"] == "g1_bad"
    assert ev.payload["detail"]    # non-empty failure reason


def test_G1_skip_preflight_emits_no_event(tmp_path):
    """When the caller explicitly opts out of preflight, no event
    is emitted - we don't want to pollute the audit chain with
    null/skipped attempts."""
    from nth_dao.attach import attach
    session = attach(
        agent_id="g1-skip-agent",
        backend=MockBackend(),
        workspace=str(tmp_path),
        start_heartbeat=False,
        skip_preflight=True,
    )
    try:
        events = _read_preflight_events(tmp_path)
        assert events == []
    finally:
        session.detach()


def test_G1_no_backend_emits_no_preflight_event(tmp_path):
    """attach(backend=None) - the preflight gate is moot."""
    from nth_dao.attach import attach
    session = attach(
        agent_id="g1-no-backend",
        backend=None,
        workspace=str(tmp_path),
        start_heartbeat=False,
    )
    try:
        events = _read_preflight_events(tmp_path)
        assert events == []
    finally:
        session.detach()


def test_G1_preflight_event_payload_contains_structured_data(tmp_path):
    """structured field is for machine-readable debug info (stdout,
    returncode etc.) - must survive serialization."""
    from nth_dao.attach import attach

    class _Backend(AgentBackend):
        backend_id = "g1_structured"

        @classmethod
        def is_available(cls, **kwargs) -> bool:
            return True

        def preflight_check(self, *, timeout=5.0):
            from team_layer.backends.base import PreflightResult
            from datetime import datetime, timezone
            return PreflightResult(
                ok=True, backend_id=self.backend_id,
                checked_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=3,
                detail="",
                structured={"stdout": "OK", "rc": 0, "extras": [1, 2, 3]},
            )

        def start_session(self, config): pass
        def send_turn(self, prompt, system_prompt=""): pass
        def end_session(self): pass

    session = attach(
        agent_id="g1-struct",
        backend=_Backend(),
        workspace=str(tmp_path),
        start_heartbeat=False,
    )
    try:
        events = _read_preflight_events(tmp_path)
        assert events
        structured = events[-1].payload["structured"]
        assert structured["stdout"] == "OK"
        assert structured["rc"] == 0
        assert structured["extras"] == [1, 2, 3]
    finally:
        session.detach()
