"""Tests for nth_dao.fault_isolation — circuit breaker + health tracking."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from nth_dao.fault_isolation import (
    FaultIsolator,
    AgentHealth,
    CircuitState,
    FailureRecord,
)


# ────────────────────────── Fixtures ──────────────────────────


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def isolator(tmp_workspace):
    return FaultIsolator(workspace=tmp_workspace)


@pytest.fixture
def fast_isolator(tmp_workspace):
    """Isolator with very fast cooldown for testing transitions."""
    return FaultIsolator(
        workspace=tmp_workspace,
        failure_threshold=3,
        cooldown_seconds=0.01,  # effectively instant
    )


# ────────────────────────── Data types ──────────────────────────


class TestAgentHealth:
    def test_defaults(self):
        h = AgentHealth(agent_id="alice")
        assert h.agent_id == "alice"
        assert h.circuit_state == CircuitState.CLOSED.value
        assert h.health_score == 1.0
        assert h.failure_count == 0
        assert h.success_count == 0


class TestFailureRecord:
    def test_fields(self):
        f = FailureRecord(
            agent_id="bob",
            action_type="deploy",
            error="timeout",
            timestamp="2026-01-01T00:00:00",
        )
        assert f.agent_id == "bob"
        assert f.action_type == "deploy"
        assert f.error == "timeout"


class TestCircuitState:
    def test_values(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


# ────────────────────────── Default state ──────────────────────────


class TestDefaultState:
    def test_unknown_agent_is_healthy(self, isolator):
        assert isolator.circuit_state("nobody") == CircuitState.CLOSED.value
        assert isolator.health_score("nobody") == 1.0
        assert not isolator.is_circuit_open("nobody")

    def test_healthy_agents_includes_unknown(self, isolator):
        assert "unknown" in isolator.healthy_agents(["unknown"])

    def test_agent_health_default(self, isolator):
        h = isolator.agent_health("new-agent")
        assert h.circuit_state == CircuitState.CLOSED.value
        assert h.health_score == 1.0


# ────────────────────────── Circuit breaker ──────────────────────────


class TestCircuitBreaker:
    def test_single_failure_does_not_open(self, isolator):
        isolator.record_failure("agent-a", "test", "error")
        assert not isolator.is_circuit_open("agent-a")

    def test_threshold_failures_open_circuit(self, isolator):
        for i in range(5):
            isolator.record_failure("agent-a", "test", f"error-{i}")
        assert isolator.is_circuit_open("agent-a")
        assert isolator.circuit_state("agent-a") == CircuitState.OPEN.value

    def test_success_does_not_open_circuit(self, isolator):
        for _ in range(10):
            isolator.record_success("agent-a", "test")
        assert not isolator.is_circuit_open("agent-a")

    def test_mixed_success_and_failure(self, isolator):
        # 2 successes, then 5 failures
        isolator.record_success("agent-a")
        isolator.record_success("agent-a")
        for i in range(5):
            isolator.record_failure("agent-a", "test", f"e{i}")
        assert isolator.is_circuit_open("agent-a")

    def test_custom_threshold(self, tmp_workspace):
        iso = FaultIsolator(workspace=tmp_workspace, failure_threshold=2)
        iso.record_failure("a", "t", "e1")
        assert not iso.is_circuit_open("a")
        iso.record_failure("a", "t", "e2")
        assert iso.is_circuit_open("a")


# ────────────────────────── Health tracking ──────────────────────────


class TestHealthTracking:
    def test_health_score_perfect(self, isolator):
        isolator.record_success("agent-a", "test")
        assert isolator.health_score("agent-a") == 1.0

    def test_health_score_degraded(self, isolator):
        isolator.record_success("agent-a")
        isolator.record_failure("agent-a", "t", "e")
        assert 0.4 <= isolator.health_score("agent-a") <= 0.6

    def test_health_score_open_penalty(self, isolator):
        for i in range(5):
            isolator.record_failure("agent-a", "t", f"e{i}")
        assert isolator.health_score("agent-a") < 0.5  # penalized

    def test_agent_health_report(self, isolator):
        isolator.record_success("agent-a", "deploy")
        isolator.record_failure("agent-a", "deploy", "timeout")
        h = isolator.agent_health("agent-a")
        assert h.success_count == 1
        assert h.failure_count == 1
        assert h.last_failure_error == "timeout"
        assert h.last_success
        assert h.last_failure

    def test_error_truncation(self, isolator):
        long_error = "x" * 1000
        isolator.record_failure("a", "t", long_error)
        h = isolator.agent_health("a")
        assert len(h.last_failure_error) <= 500


# ────────────────────────── Healthy agents filter ──────────────────────────


class TestHealthyAgents:
    def test_all_healthy(self, isolator):
        result = isolator.healthy_agents(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_excludes_open(self, isolator):
        for i in range(5):
            isolator.record_failure("bad-agent", "t", f"e{i}")
        result = isolator.healthy_agents(["good", "bad-agent"])
        assert result == ["good"]

    def test_empty_input(self, isolator):
        assert isolator.healthy_agents([]) == []


# ────────────────────────── Reset ──────────────────────────


class TestReset:
    def test_reset_single(self, isolator):
        for i in range(5):
            isolator.record_failure("agent-a", "t", "e")
        assert isolator.is_circuit_open("agent-a")
        isolator.reset("agent-a")
        assert not isolator.is_circuit_open("agent-a")
        assert isolator.health_score("agent-a") == 1.0

    def test_reset_all(self, isolator):
        for i in range(5):
            isolator.record_failure("a1", "t", "e")
            isolator.record_failure("a2", "t", "e")
        isolator.reset_all()
        assert not isolator.is_circuit_open("a1")
        assert not isolator.is_circuit_open("a2")

    def test_reset_clears_failures(self, isolator):
        for i in range(5):
            isolator.record_failure("a", "t", "e")
        isolator.reset("a")
        h = isolator.agent_health("a")
        assert h.failure_count == 0
        assert h.consecutive_failures == 0


# ────────────────────────── Persistence ──────────────────────────


class TestPersistence:
    def test_state_survives_new_instance(self, tmp_workspace):
        iso1 = FaultIsolator(workspace=tmp_workspace)
        for i in range(5):
            iso1.record_failure("a", "t", "e")
        assert iso1.is_circuit_open("a")

        # New instance loads from disk
        iso2 = FaultIsolator(workspace=tmp_workspace)
        assert iso2.is_circuit_open("a")
        assert iso2.health_score("a") < 0.5

    def test_state_file_created(self, isolator):
        isolator.record_failure("a", "t", "e")
        state_file = isolator._state_path()
        assert state_file.exists()


# ────────────────────────── Half-open / recovery ──────────────────────────


class TestHalfOpenRecovery:
    def test_open_transitions_to_half_open_after_cooldown(self, tmp_workspace):
        iso = FaultIsolator(
            workspace=tmp_workspace,
            failure_threshold=3,
            cooldown_seconds=0.001,
            success_threshold=1,
        )
        for i in range(3):
            iso.record_failure("a", "t", "e")
        assert iso.circuit_state("a") == CircuitState.OPEN.value

        # Wait for cooldown
        time.sleep(0.01)
        # State should auto-transition to half_open on next query
        assert iso.circuit_state("a") == CircuitState.HALF_OPEN.value

    def test_success_in_half_open_closes_circuit(self, tmp_workspace):
        iso = FaultIsolator(
            workspace=tmp_workspace,
            failure_threshold=3,
            cooldown_seconds=0.001,
            success_threshold=1,
        )
        for i in range(3):
            iso.record_failure("a", "t", "e")
        time.sleep(0.01)
        # Probe: record success
        iso.record_success("a", "probe")
        assert iso.circuit_state("a") == CircuitState.CLOSED.value

    def test_failure_in_half_open_returns_to_open(self, tmp_workspace):
        iso = FaultIsolator(
            workspace=tmp_workspace,
            failure_threshold=3,
            cooldown_seconds=0.001,
            success_threshold=2,
        )
        for i in range(3):
            iso.record_failure("a", "t", "e")
        time.sleep(0.01)
        assert iso.circuit_state("a") == CircuitState.HALF_OPEN.value

        # Fail the probe
        iso.record_failure("a", "probe", "still-failing")
        assert iso.circuit_state("a") == CircuitState.OPEN.value


# ────────────────────────── All health ──────────────────────────


class TestAllHealth:
    def test_returns_all_agents(self, isolator):
        isolator.record_success("a")
        isolator.record_failure("b", "t", "e")
        all_h = isolator.all_health()
        assert "a" in all_h
        assert "b" in all_h

    def test_empty(self, isolator):
        assert isolator.all_health() == {}


# ────────────────────────── Edge cases ──────────────────────────


class TestEdgeCases:
    def test_repr(self, isolator):
        r = repr(isolator)
        assert "FaultIsolator" in r
        assert "open=0" in r

    def test_repr_with_open(self, isolator):
        for i in range(5):
            isolator.record_failure("a", "t", "e")
        r = repr(isolator)
        assert "open=1" in r

    def test_empty_agent_id(self, isolator):
        # Should not crash
        isolator.record_success("")
        isolator.record_failure("", "", "")
        assert isolator.circuit_state("") == CircuitState.CLOSED.value

    def test_rapid_recordings(self, isolator):
        """Ensure no race conditions with rapid success/failure alternation."""
        for _ in range(20):
            isolator.record_success("a")
            isolator.record_failure("a", "t", "e")
        # Should not crash; health may be degraded
        assert 0.0 <= isolator.health_score("a") <= 1.0
