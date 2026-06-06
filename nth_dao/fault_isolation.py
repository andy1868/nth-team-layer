"""
Fault Isolator — circuit breaker + health tracking for multi-agent systems.

Prevents cascading failures by detecting unhealthy agents and temporarily
removing them from the routing pool.  Implements the standard three-state
circuit breaker pattern (CLOSED → OPEN → HALF_OPEN → CLOSED) with
configurable thresholds and automatic recovery probing.

States
------

    CLOSED ──(failure_threshold reached)──→ OPEN
    OPEN   ──(cooldown elapsed)──────────→ HALF_OPEN
    HALF_OPEN ──(probe succeeds)──────────→ CLOSED
    HALF_OPEN ──(probe fails)─────────────→ OPEN

Usage::

    isolator = FaultIsolator(workspace=team.workspace)

    # After each agent interaction:
    isolator.record_success("agent-bob", "code_review")
    isolator.record_failure("agent-carol", "deploy", "timeout")

    # Before routing:
    if isolator.is_circuit_open("agent-carol"):
        # don't route — circuit is open
        pass

    # Get healthy agents for PeerFinder:
    healthy = isolator.healthy_agents(all_agents)

Design
------

- Zero external dependencies — pure stdlib.
- Time-windowed failure counting (old failures expire).
- Per-agent circuit breaker state.
- State persisted as JSON (survives restarts).
- Thread-safe for concurrent recording.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .util.io import atomic_write_json, safe_load_json

logger = logging.getLogger("nth_dao.fault_isolation")


# ────────────────────────── Enums ──────────────────────────


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"          # healthy — route normally
    OPEN = "open"              # failing — do NOT route
    HALF_OPEN = "half_open"    # testing — allow one probe


# ────────────────────────── Data types ──────────────────────────


@dataclass
class FailureRecord:
    """A single failure event."""
    agent_id: str
    action_type: str
    error: str
    timestamp: str


@dataclass
class AgentHealth:
    """Aggregate health information for one agent."""
    agent_id: str
    circuit_state: str = CircuitState.CLOSED.value
    failure_count: int = 0
    success_count: int = 0
    consecutive_failures: int = 0
    last_failure: str = ""
    last_success: str = ""
    last_failure_error: str = ""
    opened_at: str = ""
    half_open_at: str = ""
    health_score: float = 1.0  # 0.0 (dead) to 1.0 (perfect)
    half_open_successes: int = 0  # successes since entering HALF_OPEN (persisted)


# ────────────────────────── Fault Isolator ──────────────────────────


class FaultIsolator:
    """Circuit breaker and health tracker for agent interactions.

    Tracks success/failure per agent and opens circuits when an agent
    exceeds the failure threshold within the configured time window.

    Parameters
    ----------
    workspace : Path
        Working directory for state persistence.
    storage_dir : str
        Subdirectory under workspace.  Default ``"team_faults"``.
    failure_threshold : int
        Consecutive failures before circuit opens.  Default 5.
    failure_window_seconds : float
        Failures older than this are pruned from counting.  Default 300 (5 min).
    cooldown_seconds : float
        Time circuit stays OPEN before transitioning to HALF_OPEN.  Default 60.
    half_open_max_probes : int
        Max consecutive HALF_OPEN probes before reverting to OPEN.  Default 1.
    success_threshold : int
        Consecutive successes needed in HALF_OPEN to close circuit.  Default 2.
    """

    DEFAULT_STORAGE_DIR = "team_faults"
    DEFAULT_FAILURE_THRESHOLD = 5
    DEFAULT_FAILURE_WINDOW = 300.0
    DEFAULT_COOLDOWN = 60.0
    DEFAULT_HALF_OPEN_MAX_PROBES = 1
    DEFAULT_SUCCESS_THRESHOLD = 2
    DEFAULT_MAX_FAILURE_RECORDS = 200

    def __init__(
        self,
        workspace: Optional[Path] = None,
        *,
        storage_dir: str = DEFAULT_STORAGE_DIR,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        failure_window_seconds: float = DEFAULT_FAILURE_WINDOW,
        cooldown_seconds: float = DEFAULT_COOLDOWN,
        half_open_max_probes: int = DEFAULT_HALF_OPEN_MAX_PROBES,
        success_threshold: int = DEFAULT_SUCCESS_THRESHOLD,
        max_failure_records: int = DEFAULT_MAX_FAILURE_RECORDS,
    ):
        self._workspace = workspace or Path.cwd()
        self._storage_dir = storage_dir
        self._failure_threshold = max(1, failure_threshold)
        self._failure_window = max(1.0, failure_window_seconds)
        self._cooldown = max(0.01, cooldown_seconds)
        self._half_open_max_probes = max(1, half_open_max_probes)
        self._success_threshold = max(1, success_threshold)
        self._max_failure_records = max(10, max_failure_records)

        self._lock = threading.Lock()

        # In-memory state (lazily loaded from disk)
        self._health: Dict[str, AgentHealth] = {}
        self._failures: Dict[str, List[FailureRecord]] = {}
        self._loaded = False

        # Ensure storage directory exists
        self._state_dir = self._workspace / storage_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)

    # ── Lazy loading ──────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        path = self._state_path()
        if path.exists():
            data = safe_load_json(path) or {}
            self._health = {}
            for agent_id, raw in data.get("health", {}).items():
                self._health[agent_id] = AgentHealth(**raw)
            self._failures = {}
            for agent_id, raw_list in data.get("failures", {}).items():
                self._failures[agent_id] = [
                    FailureRecord(**r) for r in raw_list
                ]
        self._loaded = True

    def _state_path(self) -> Path:
        return self._state_dir / "fault_state.json"

    def _persist(self) -> None:
        data = {
            "health": {
                agent_id: {
                    "agent_id": h.agent_id,
                    "circuit_state": h.circuit_state,
                    "failure_count": h.failure_count,
                    "success_count": h.success_count,
                    "consecutive_failures": h.consecutive_failures,
                    "last_failure": h.last_failure,
                    "last_success": h.last_success,
                    "last_failure_error": h.last_failure_error,
                    "opened_at": h.opened_at,
                    "half_open_at": h.half_open_at,
                    "health_score": h.health_score,
                    "half_open_successes": h.half_open_successes,
                }
                for agent_id, h in self._health.items()
            },
            "failures": {
                agent_id: [
                    {
                        "agent_id": f.agent_id,
                        "action_type": f.action_type,
                        "error": f.error,
                        "timestamp": f.timestamp,
                    }
                    for f in failures
                ]
                for agent_id, failures in self._failures.items()
            },
        }
        atomic_write_json(self._state_path(), data)

    # ── Recording ─────────────────────────────────────────

    def record_success(self, agent_id: str, action_type: str = "") -> None:
        """Record a successful interaction with *agent_id*.

        May transition HALF_OPEN → CLOSED if enough consecutive successes.
        """
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = datetime.now().isoformat()

            h.success_count += 1
            h.last_success = now

            # Auto-transition OPEN → HALF_OPEN (if cooldown elapsed)
            # before processing the success, so the success can close it
            self._maybe_transition(agent_id)

            if h.circuit_state == CircuitState.HALF_OPEN.value:
                h.consecutive_failures = 0
                h.half_open_successes += 1
                if h.half_open_successes >= self._success_threshold:
                    h.circuit_state = CircuitState.CLOSED.value
                    h.opened_at = ""
                    h.half_open_at = ""
                    h.consecutive_failures = 0
                    h.half_open_successes = 0
                    h.health_score = min(1.0, h.health_score + 0.2)
                    logger.info("circuit CLOSED for %r (recovered)", agent_id)

            self._update_health_score(h)
            self._persist()

    def record_failure(
        self, agent_id: str, action_type: str = "", error: str = ""
    ) -> None:
        """Record a failed interaction with *agent_id*.

        May transition CLOSED → OPEN or HALF_OPEN → OPEN.
        """
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = datetime.now().isoformat()

            h.failure_count += 1
            h.consecutive_failures += 1
            h.last_failure = now
            h.last_failure_error = error[:500]  # truncate long errors

            flist = self._failures.setdefault(agent_id, [])
            flist.append(FailureRecord(
                agent_id=agent_id,
                action_type=action_type,
                error=error[:500],
                timestamp=now,
            ))
            # Bound: keep only the most recent records
            if len(flist) > self._max_failure_records:
                self._failures[agent_id] = flist[-self._max_failure_records:]

            # Check if circuit should open
            recent = self._recent_failures(agent_id)
            if len(recent) >= self._failure_threshold:
                if h.circuit_state == CircuitState.CLOSED.value:
                    h.circuit_state = CircuitState.OPEN.value
                    h.opened_at = now
                    logger.warning(
                        "circuit OPEN for %r (%d failures in %.0fs)",
                        agent_id, len(recent), self._failure_window,
                    )
                elif h.circuit_state == CircuitState.HALF_OPEN.value:
                    h.circuit_state = CircuitState.OPEN.value
                    h.opened_at = now
                    h.half_open_at = ""
                    logger.warning(
                        "circuit re-OPEN for %r (half-open probe failed)",
                        agent_id,
                    )

            self._update_health_score(h)
            self._persist()

    # ── Circuit queries ───────────────────────────────────

    def is_circuit_open(self, agent_id: str) -> bool:
        """Return True if the circuit for *agent_id* is OPEN (do not route)."""
        with self._lock:
            self._ensure_loaded()
            # Auto-transition OPEN → HALF_OPEN if cooldown elapsed
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            if h is None:
                return False
            return h.circuit_state == CircuitState.OPEN.value

    def circuit_state(self, agent_id: str) -> str:
        """Return the current circuit state for *agent_id*."""
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            return h.circuit_state if h else CircuitState.CLOSED.value

    def health_score(self, agent_id: str) -> float:
        """Return 0.0–1.0 health score for *agent_id*."""
        with self._lock:
            self._ensure_loaded()
            h = self._health.get(agent_id)
            return h.health_score if h else 1.0

    def agent_health(self, agent_id: str) -> AgentHealth:
        """Return full health report for *agent_id*."""
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            return self._get_or_create_health(agent_id)

    def healthy_agents(self, agent_ids: List[str]) -> List[str]:
        """Filter *agent_ids* to those with CLOSED or HALF_OPEN circuits."""
        with self._lock:
            self._ensure_loaded()
            result = []
            for aid in agent_ids:
                self._maybe_transition(aid)
                h = self._health.get(aid)
                if h is None or h.circuit_state != CircuitState.OPEN.value:
                    result.append(aid)
            return result

    def all_health(self) -> Dict[str, AgentHealth]:
        """Return health reports for all tracked agents."""
        with self._lock:
            self._ensure_loaded()
            for aid in list(self._health):
                self._maybe_transition(aid)
            return dict(self._health)

    # ── Management ────────────────────────────────────────

    def reset(self, agent_id: str) -> None:
        """Manually reset circuit for *agent_id* to CLOSED."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            h.circuit_state = CircuitState.CLOSED.value
            h.failure_count = 0
            h.consecutive_failures = 0
            h.last_failure = ""
            h.last_failure_error = ""
            h.opened_at = ""
            h.half_open_at = ""
            h.health_score = 1.0
            self._failures.pop(agent_id, None)
            self._persist()
            logger.info("circuit RESET for %r", agent_id)

    def reset_all(self) -> None:
        """Reset all circuits to CLOSED."""
        with self._lock:
            self._ensure_loaded()
            for h in self._health.values():
                h.circuit_state = CircuitState.CLOSED.value
                h.failure_count = 0
                h.consecutive_failures = 0
                h.last_failure = ""
                h.last_failure_error = ""
                h.opened_at = ""
                h.half_open_at = ""
                h.health_score = 1.0
            self._failures.clear()
            self._persist()
            logger.info("all circuits RESET")

    # ── Internals ─────────────────────────────────────────

    def _get_or_create_health(self, agent_id: str) -> AgentHealth:
        if agent_id not in self._health:
            self._health[agent_id] = AgentHealth(agent_id=agent_id)
        return self._health[agent_id]

    def _maybe_transition(self, agent_id: str) -> None:
        """Check if OPEN circuit should transition to HALF_OPEN."""
        h = self._health.get(agent_id)
        if h is None or h.circuit_state != CircuitState.OPEN.value:
            return
        if not h.opened_at:
            return
        try:
            opened = datetime.fromisoformat(h.opened_at)
            elapsed = (datetime.now() - opened).total_seconds()
            if elapsed >= self._cooldown:
                h.circuit_state = CircuitState.HALF_OPEN.value
                h.half_open_at = datetime.now().isoformat()
                h.consecutive_failures = 0
                logger.info("circuit HALF_OPEN for %r (cooldown elapsed)", agent_id)
                self._persist()
        except (ValueError, TypeError):
            pass

    def _recent_failures(self, agent_id: str) -> List[FailureRecord]:
        """Return failures within the time window, auto-pruning old ones."""
        failures = self._failures.get(agent_id, [])
        if not failures:
            return []
        now = datetime.now()
        cutoff = now.timestamp() - self._failure_window
        recent = []
        for f in failures:
            try:
                ts = datetime.fromisoformat(f.timestamp).timestamp()
                if ts >= cutoff:
                    recent.append(f)
            except (ValueError, TypeError):
                continue
        # Prune in-place
        self._failures[agent_id] = recent
        return recent

    def _update_health_score(self, h: AgentHealth) -> None:
        """Compute health score from success/failure ratio."""
        total = h.success_count + h.failure_count
        if total == 0:
            h.health_score = 1.0
            return
        ratio = h.success_count / total
        # Penalize open circuits
        if h.circuit_state == CircuitState.OPEN.value:
            ratio *= 0.3
        elif h.circuit_state == CircuitState.HALF_OPEN.value:
            ratio *= 0.6
        h.health_score = round(max(0.0, min(1.0, ratio)), 3)

    def __repr__(self) -> str:
        with self._lock:
            self._ensure_loaded()
            open_count = sum(
                1 for h in self._health.values()
                if h.circuit_state == CircuitState.OPEN.value
            )
            return (
                f"FaultIsolator(agents={len(self._health)}, "
                f"open={open_count})"
            )
