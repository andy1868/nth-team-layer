"""FaultIsolator — circuit breaker + health tracking for multi-agent systems.

Prevents cascading failures by detecting unhealthy agents and temporarily
removing them from the routing pool. Three-state circuit breaker with
time-windowed failure counting, automatic recovery probing, and
**signed audit events** on every state transition so that a malicious
actor can't silently weaponise the breaker to censor a peer.

States
------

    CLOSED ──(failure_threshold reached)──→ OPEN
    OPEN   ──(cooldown elapsed)──────────→ HALF_OPEN
    HALF_OPEN ──(probe succeeds)──────────→ CLOSED
    HALF_OPEN ──(probe fails)─────────────→ OPEN

Usage::

    bus = EventBus(workspace, identity=alice)
    isolator = FaultIsolator(workspace=workspace, event_bus=bus)

    isolator.record_success("agent-bob", "code_review")
    isolator.record_failure("agent-carol", "deploy", "timeout")

    if isolator.is_circuit_open("agent-carol"):
        ...  # circuit is open — don't route

    healthy = isolator.healthy_agents(["bob", "carol", "dave"])

Audit events emitted (when ``event_bus`` is configured):
  - ``circuit.opened``       payload: {agent_id, prev_state, reason}
  - ``circuit.half_opened``  payload: {agent_id, opened_for_seconds}
  - ``circuit.closed``       payload: {agent_id, prev_state, reason}

These events sign with the bus's identity, so any consumer replaying
the EventBus stream can detect a peer being repeatedly forced into
OPEN state (censorship signal) and act on it.

Design contributed by @andy1868 in the agent-collab submission
(June 2026). This implementation preserves the three-state machine,
time-windowed failure counting and persistence model, while fixing
two issues from the original:

1. Original set ``_half_open_successes`` as a dynamic attribute on the
   ``AgentHealth`` dataclass — it didn't survive across process
   reload because the serialised dict had no key for it. Promoted
   here to a real dataclass field.

2. Original had no audit-trail integration. Without it, repeated
   failure injection that censors a peer leaves no signed evidence
   on EventBus, defeating the "signatures not trust assertions"
   principle (NTH DAO P4). All three state transitions now emit
   signed events.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .util.io import atomic_write_json, safe_load_json

if TYPE_CHECKING:
    from .event_bus import EventBus

logger = logging.getLogger("nth_dao.fault_isolation")


class CircuitState(str, Enum):
    CLOSED = "closed"          # healthy — route normally
    OPEN = "open"              # failing — do NOT route
    HALF_OPEN = "half_open"    # testing — allow probes


@dataclass
class FailureRecord:
    agent_id: str
    action_type: str
    error: str
    timestamp: str


@dataclass
class AgentHealth:
    """Aggregate health information for one agent.

    All fields are declared on the dataclass so they survive
    persist → reload round trips. The previous design stored
    ``_half_open_successes`` as a dynamic attribute, which evaporated
    on reload — promoted here to a real field.
    """

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
    half_open_successes: int = 0   # promoted from dynamic attr (was a reload bug)
    health_score: float = 1.0      # 0.0 (dead) to 1.0 (perfect)


class FaultIsolator:
    """Circuit breaker and health tracker for agent interactions."""

    DEFAULT_STORAGE_DIR = "team_faults"
    DEFAULT_FAILURE_THRESHOLD = 5
    DEFAULT_FAILURE_WINDOW = 300.0
    DEFAULT_COOLDOWN = 60.0
    DEFAULT_HALF_OPEN_MAX_PROBES = 1
    DEFAULT_SUCCESS_THRESHOLD = 2

    def __init__(
        self,
        workspace: Optional[Path] = None,
        *,
        event_bus: Optional["EventBus"] = None,
        storage_dir: str = DEFAULT_STORAGE_DIR,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        failure_window_seconds: float = DEFAULT_FAILURE_WINDOW,
        cooldown_seconds: float = DEFAULT_COOLDOWN,
        half_open_max_probes: int = DEFAULT_HALF_OPEN_MAX_PROBES,
        success_threshold: int = DEFAULT_SUCCESS_THRESHOLD,
    ):
        self._workspace = workspace or Path.cwd()
        self._storage_dir = storage_dir
        self._event_bus = event_bus
        self._failure_threshold = max(1, failure_threshold)
        self._failure_window = max(1.0, failure_window_seconds)
        self._cooldown = max(0.01, cooldown_seconds)
        self._half_open_max_probes = max(1, half_open_max_probes)
        self._success_threshold = max(1, success_threshold)

        self._lock = threading.Lock()
        self._health: Dict[str, AgentHealth] = {}
        self._failures: Dict[str, List[FailureRecord]] = {}
        self._loaded = False

        self._state_dir = self._workspace / storage_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)

    # ── Persistence ───────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        path = self._state_path()
        if path.exists():
            data = safe_load_json(path) or {}
            self._health = {}
            valid_fields = {f.name for f in fields(AgentHealth)}
            for agent_id, raw in data.get("health", {}).items():
                # Filter unknown keys so a future schema bump can't crash a
                # reload from an older state file.
                filtered = {k: v for k, v in raw.items() if k in valid_fields}
                self._health[agent_id] = AgentHealth(**filtered)
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
                agent_id: asdict(h)
                for agent_id, h in self._health.items()
            },
            "failures": {
                agent_id: [asdict(f) for f in failures]
                for agent_id, failures in self._failures.items()
            },
        }
        atomic_write_json(self._state_path(), data)

    # ── Recording ─────────────────────────────────────────

    def record_success(self, agent_id: str, action_type: str = "") -> None:
        """Record a successful interaction with *agent_id*.

        May transition HALF_OPEN → CLOSED if enough consecutive successes."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = datetime.now().isoformat()

            h.success_count += 1
            h.last_success = now

            # Auto-transition OPEN → HALF_OPEN (if cooldown elapsed)
            # *before* processing the success so it can close the circuit.
            self._maybe_transition(agent_id)

            if h.circuit_state == CircuitState.HALF_OPEN.value:
                h.consecutive_failures = 0
                h.half_open_successes += 1
                if h.half_open_successes >= self._success_threshold:
                    prev = h.circuit_state
                    h.circuit_state = CircuitState.CLOSED.value
                    h.opened_at = ""
                    h.half_open_at = ""
                    h.half_open_successes = 0
                    h.consecutive_failures = 0
                    h.health_score = min(1.0, h.health_score + 0.2)
                    logger.info("circuit CLOSED for %r (recovered)", agent_id)
                    self._emit_event("circuit.closed", {
                        "agent_id": agent_id,
                        "prev_state": prev,
                        "reason": "half_open_probes_succeeded",
                    })

            self._update_health_score(h)
            self._persist()

    def record_failure(
        self, agent_id: str, action_type: str = "", error: str = ""
    ) -> None:
        """Record a failed interaction with *agent_id*.

        May transition CLOSED → OPEN or HALF_OPEN → OPEN."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = datetime.now().isoformat()

            h.failure_count += 1
            h.consecutive_failures += 1
            h.last_failure = now
            h.last_failure_error = error[:500]

            self._failures.setdefault(agent_id, []).append(
                FailureRecord(
                    agent_id=agent_id,
                    action_type=action_type,
                    error=error[:500],
                    timestamp=now,
                )
            )

            recent = self._recent_failures(agent_id)
            if len(recent) >= self._failure_threshold:
                if h.circuit_state == CircuitState.CLOSED.value:
                    h.circuit_state = CircuitState.OPEN.value
                    h.opened_at = now
                    h.half_open_successes = 0
                    logger.warning(
                        "circuit OPEN for %r (%d failures in %.0fs)",
                        agent_id, len(recent), self._failure_window,
                    )
                    self._emit_event("circuit.opened", {
                        "agent_id": agent_id,
                        "prev_state": CircuitState.CLOSED.value,
                        "failure_count": len(recent),
                        "window_seconds": self._failure_window,
                        "reason": "threshold_exceeded",
                    })
                elif h.circuit_state == CircuitState.HALF_OPEN.value:
                    h.circuit_state = CircuitState.OPEN.value
                    h.opened_at = now
                    h.half_open_at = ""
                    h.half_open_successes = 0
                    logger.warning(
                        "circuit re-OPEN for %r (half-open probe failed)",
                        agent_id,
                    )
                    self._emit_event("circuit.opened", {
                        "agent_id": agent_id,
                        "prev_state": CircuitState.HALF_OPEN.value,
                        "reason": "half_open_probe_failed",
                    })

            self._update_health_score(h)
            self._persist()

    # ── Queries ───────────────────────────────────────────

    def is_circuit_open(self, agent_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            return bool(h and h.circuit_state == CircuitState.OPEN.value)

    def circuit_state(self, agent_id: str) -> str:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            return h.circuit_state if h else CircuitState.CLOSED.value

    def health_score(self, agent_id: str) -> float:
        with self._lock:
            self._ensure_loaded()
            h = self._health.get(agent_id)
            return h.health_score if h else 1.0

    def agent_health(self, agent_id: str) -> AgentHealth:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            return self._get_or_create_health(agent_id)

    def healthy_agents(self, agent_ids: List[str]) -> List[str]:
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
        with self._lock:
            self._ensure_loaded()
            for aid in list(self._health):
                self._maybe_transition(aid)
            return dict(self._health)

    # ── Management ────────────────────────────────────────

    def reset(self, agent_id: str) -> None:
        """Manually reset circuit for *agent_id* to CLOSED.

        Emits a ``circuit.closed`` event with reason ``"manual_reset"``
        so the audit trail records who closed it and when."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            prev = h.circuit_state
            h.circuit_state = CircuitState.CLOSED.value
            h.failure_count = 0
            h.consecutive_failures = 0
            h.half_open_successes = 0
            h.last_failure = ""
            h.last_failure_error = ""
            h.opened_at = ""
            h.half_open_at = ""
            h.health_score = 1.0
            self._failures.pop(agent_id, None)
            self._persist()
            logger.info("circuit RESET for %r", agent_id)
            if prev != CircuitState.CLOSED.value:
                self._emit_event("circuit.closed", {
                    "agent_id": agent_id,
                    "prev_state": prev,
                    "reason": "manual_reset",
                })

    def reset_all(self) -> None:
        with self._lock:
            self._ensure_loaded()
            for aid, h in list(self._health.items()):
                prev = h.circuit_state
                h.circuit_state = CircuitState.CLOSED.value
                h.failure_count = 0
                h.consecutive_failures = 0
                h.half_open_successes = 0
                h.last_failure = ""
                h.last_failure_error = ""
                h.opened_at = ""
                h.half_open_at = ""
                h.health_score = 1.0
                if prev != CircuitState.CLOSED.value:
                    self._emit_event("circuit.closed", {
                        "agent_id": aid,
                        "prev_state": prev,
                        "reason": "reset_all",
                    })
            self._failures.clear()
            self._persist()
            logger.info("all circuits RESET")

    # ── Internals ─────────────────────────────────────────

    def _get_or_create_health(self, agent_id: str) -> AgentHealth:
        if agent_id not in self._health:
            self._health[agent_id] = AgentHealth(agent_id=agent_id)
        return self._health[agent_id]

    def _maybe_transition(self, agent_id: str) -> None:
        """OPEN → HALF_OPEN once cooldown elapsed; emits the audit event."""
        h = self._health.get(agent_id)
        if h is None or h.circuit_state != CircuitState.OPEN.value:
            return
        if not h.opened_at:
            return
        try:
            opened = datetime.fromisoformat(h.opened_at)
        except (ValueError, TypeError):
            return
        elapsed = (datetime.now() - opened).total_seconds()
        if elapsed < self._cooldown:
            return
        h.circuit_state = CircuitState.HALF_OPEN.value
        h.half_open_at = datetime.now().isoformat()
        h.half_open_successes = 0
        h.consecutive_failures = 0
        logger.info("circuit HALF_OPEN for %r (cooldown elapsed)", agent_id)
        self._persist()
        self._emit_event("circuit.half_opened", {
            "agent_id": agent_id,
            "opened_for_seconds": round(elapsed, 1),
        })

    def _recent_failures(self, agent_id: str) -> List[FailureRecord]:
        failures = self._failures.get(agent_id, [])
        if not failures:
            return []
        cutoff = datetime.now().timestamp() - self._failure_window
        recent = []
        for f in failures:
            try:
                ts = datetime.fromisoformat(f.timestamp).timestamp()
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                recent.append(f)
        self._failures[agent_id] = recent
        return recent

    def _update_health_score(self, h: AgentHealth) -> None:
        total = h.success_count + h.failure_count
        if total == 0:
            h.health_score = 1.0
            return
        ratio = h.success_count / total
        if h.circuit_state == CircuitState.OPEN.value:
            ratio *= 0.3
        elif h.circuit_state == CircuitState.HALF_OPEN.value:
            ratio *= 0.6
        h.health_score = round(max(0.0, min(1.0, ratio)), 3)

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Audit-trail emit. Swallows EventBus errors so a downed bus
        cannot freeze the fault path itself — defence in depth."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.emit(event_type, payload)
        except Exception as exc:   # noqa: BLE001
            logger.warning("fault_isolation EventBus emit failed: %s", exc)

    def __repr__(self) -> str:
        with self._lock:
            self._ensure_loaded()
            open_count = sum(
                1 for h in self._health.values()
                if h.circuit_state == CircuitState.OPEN.value
            )
            return f"FaultIsolator(agents={len(self._health)}, open={open_count})"


__all__ = [
    "CircuitState",
    "FailureRecord",
    "AgentHealth",
    "FaultIsolator",
]
