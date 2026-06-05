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
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple, TYPE_CHECKING

from .util import now_iso
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
        # Floor is small (10ms) so unit tests can use sub-second windows;
        # production deployments will configure something sensible (minutes).
        self._failure_window = max(0.01, failure_window_seconds)
        self._cooldown = max(0.01, cooldown_seconds)
        self._half_open_max_probes = max(1, half_open_max_probes)
        self._success_threshold = max(1, success_threshold)

        self._lock = threading.Lock()
        self._health: Dict[str, AgentHealth] = {}
        # H-11 fix: bounded ring buffer per agent so a quiet agent's old
        # failures don't linger in memory forever. Capacity is sized so
        # failure_threshold's worth of events always fits, with headroom.
        self._failures_cap = max(failure_threshold * 4, 32)
        self._failures: Dict[str, Deque[FailureRecord]] = {}
        # H-1 fix: events to emit AFTER releasing the lock. Holding the
        # lock across EventBus.emit() (which takes its own file lock + fsync)
        # serialises all fault recording across the team and risks deadlock
        # if a future EventBus consumer ever calls back into us.
        self._pending_events: List[Tuple[str, Dict[str, Any]]] = []
        # H-10 fix: dirty flag — persist only on STATE TRANSITIONS, not on
        # every record_*. Transitions are the audit-meaningful events;
        # per-record stats can rebuild from the event stream if we crash.
        self._dirty = False
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
                if not isinstance(raw, dict):
                    continue
                # Filter unknown keys so a future schema bump can't crash a
                # reload from an older state file.
                filtered = {k: v for k, v in raw.items() if k in valid_fields}
                try:
                    self._health[agent_id] = AgentHealth(**filtered)
                except TypeError as exc:
                    logger.warning("dropping corrupt health entry %r: %s", agent_id, exc)
            self._failures = {}
            for agent_id, raw_list in data.get("failures", {}).items():
                if not isinstance(raw_list, list):
                    continue
                dq: Deque[FailureRecord] = deque(maxlen=self._failures_cap)
                for r in raw_list:
                    if not isinstance(r, dict):
                        continue
                    try:
                        dq.append(FailureRecord(**r))
                    except TypeError:
                        continue
                self._failures[agent_id] = dq
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
        self._dirty = False

    def _persist_if_dirty(self) -> None:
        """H-10 fix: only write to disk when something audit-meaningful
        changed (a state transition, a manual reset). Per-record stats
        are kept in memory; on crash they rebuild from the EventBus
        stream's signed circuit.* events."""
        if self._dirty:
            self._persist()

    # ── Recording ─────────────────────────────────────────

    def record_success(self, agent_id: str, action_type: str = "") -> None:
        """Record a successful interaction with *agent_id*.

        May transition HALF_OPEN → CLOSED if enough consecutive successes."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = now_iso()

            h.success_count += 1
            h.last_success = now

            # Auto-transition OPEN → HALF_OPEN (if cooldown elapsed)
            # *before* processing the success so it can close the circuit.
            self._maybe_transition(agent_id)

            if h.circuit_state == CircuitState.HALF_OPEN.value:
                h.consecutive_failures = 0
                h.half_open_successes += 1
                # Recovery progress IS audit-meaningful; persist each step
                # so a crash mid-recovery doesn't reset the probe counter.
                self._dirty = True
                if h.half_open_successes >= self._success_threshold:
                    prev = h.circuit_state
                    h.circuit_state = CircuitState.CLOSED.value
                    h.opened_at = ""
                    h.half_open_at = ""
                    h.half_open_successes = 0
                    h.consecutive_failures = 0
                    h.health_score = min(1.0, h.health_score + 0.2)
                    logger.info("circuit CLOSED for %r (recovered)", agent_id)
                    self._defer_event("circuit.closed", {
                        "agent_id": agent_id,
                        "prev_state": prev,
                        "reason": "half_open_probes_succeeded",
                    })
                    self._dirty = True

            self._update_health_score(h)
            self._persist_if_dirty()
        # H-1 fix: emit OUTSIDE the lock.
        self._drain_pending_events()

    def record_failure(
        self, agent_id: str, action_type: str = "", error: str = ""
    ) -> None:
        """Record a failed interaction with *agent_id*.

        May transition CLOSED → OPEN or HALF_OPEN → OPEN."""
        with self._lock:
            self._ensure_loaded()
            h = self._get_or_create_health(agent_id)
            now = now_iso()

            h.failure_count += 1
            h.consecutive_failures += 1
            h.last_failure = now
            h.last_failure_error = error[:500]

            self._failures.setdefault(
                agent_id, deque(maxlen=self._failures_cap),
            ).append(
                FailureRecord(
                    agent_id=agent_id,
                    action_type=action_type,
                    error=error[:500],
                    timestamp=now,
                )
            )

            # P3: emit every failure as a signed audit event so that on
            # process restart the counter can be reconstructed from the
            # EventBus stream. Without this, an attacker could repeatedly
            # crash the service to evade the threshold (H-10 deferred
            # persist only writes on transitions).
            self._defer_event("failure.observed", {
                "agent_id": agent_id,
                "action_type": action_type,
                "error": error[:500],
                "timestamp": now,
            })

            recent = self._recent_failures(agent_id)
            if len(recent) >= self._failure_threshold:
                if h.circuit_state == CircuitState.CLOSED.value:
                    h.circuit_state = CircuitState.OPEN.value
                    h.opened_at = now
                    h.half_open_successes = 0
                    self._dirty = True
                    logger.warning(
                        "circuit OPEN for %r (%d failures in %.0fs)",
                        agent_id, len(recent), self._failure_window,
                    )
                    self._defer_event("circuit.opened", {
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
                    self._dirty = True
                    logger.warning(
                        "circuit re-OPEN for %r (half-open probe failed)",
                        agent_id,
                    )
                    self._defer_event("circuit.opened", {
                        "agent_id": agent_id,
                        "prev_state": CircuitState.HALF_OPEN.value,
                        "reason": "half_open_probe_failed",
                    })

            self._update_health_score(h)
            self._persist_if_dirty()
        # H-1 fix: emit OUTSIDE the lock.
        self._drain_pending_events()

    # ── Queries ───────────────────────────────────────────

    def is_circuit_open(self, agent_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            result = bool(h and h.circuit_state == CircuitState.OPEN.value)
        self._drain_pending_events()
        return result

    def circuit_state(self, agent_id: str) -> str:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            h = self._health.get(agent_id)
            result = h.circuit_state if h else CircuitState.CLOSED.value
        self._drain_pending_events()
        return result

    def health_score(self, agent_id: str) -> float:
        with self._lock:
            self._ensure_loaded()
            h = self._health.get(agent_id)
            return h.health_score if h else 1.0

    def agent_health(self, agent_id: str) -> AgentHealth:
        with self._lock:
            self._ensure_loaded()
            self._maybe_transition(agent_id)
            result = self._get_or_create_health(agent_id)
        self._drain_pending_events()
        return result

    def healthy_agents(self, agent_ids: List[str]) -> List[str]:
        with self._lock:
            self._ensure_loaded()
            out = []
            for aid in agent_ids:
                self._maybe_transition(aid)
                h = self._health.get(aid)
                if h is None or h.circuit_state != CircuitState.OPEN.value:
                    out.append(aid)
        self._drain_pending_events()
        return out

    def all_health(self) -> Dict[str, AgentHealth]:
        with self._lock:
            self._ensure_loaded()
            for aid in list(self._health):
                self._maybe_transition(aid)
            out = dict(self._health)
        self._drain_pending_events()
        return out

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
            self._dirty = True
            self._persist()
            logger.info("circuit RESET for %r", agent_id)
            if prev != CircuitState.CLOSED.value:
                self._defer_event("circuit.closed", {
                    "agent_id": agent_id,
                    "prev_state": prev,
                    "reason": "manual_reset",
                })
        self._drain_pending_events()

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
                    self._defer_event("circuit.closed", {
                        "agent_id": aid,
                        "prev_state": prev,
                        "reason": "reset_all",
                    })
            self._failures.clear()
            self._dirty = True
            self._persist()
            logger.info("all circuits RESET")
        self._drain_pending_events()

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
        elapsed = (datetime.now(timezone.utc) - opened).total_seconds()
        if elapsed < self._cooldown:
            return
        h.circuit_state = CircuitState.HALF_OPEN.value
        h.half_open_at = now_iso()
        h.half_open_successes = 0
        h.consecutive_failures = 0
        self._dirty = True
        logger.info("circuit HALF_OPEN for %r (cooldown elapsed)", agent_id)
        self._persist_if_dirty()
        self._defer_event("circuit.half_opened", {
            "agent_id": agent_id,
            "opened_for_seconds": round(elapsed, 1),
        })

    def _recent_failures(self, agent_id: str) -> List[FailureRecord]:
        """Return failures inside the time window, pruning expired ones.

        H-11 fix: backing store is a deque(maxlen=...) so a flood of
        failures can never grow memory unbounded; expired entries are
        also drained from the left here on each call so a quiet agent
        doesn't keep ancient failures around forever."""
        failures = self._failures.get(agent_id)
        if not failures:
            return []
        cutoff = datetime.now(timezone.utc).timestamp() - self._failure_window
        # Pop expired from the LEFT of the deque (O(1) per pop).
        while failures:
            try:
                ts = datetime.fromisoformat(failures[0].timestamp).timestamp()
            except (ValueError, TypeError):
                failures.popleft()
                continue
            if ts < cutoff:
                failures.popleft()
            else:
                break
        return list(failures)

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

    def _defer_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Queue an audit event for emission AFTER the lock is released.

        H-1 fix: holding ``self._lock`` across ``EventBus.emit()`` would
        serialise the fault path against the EventBus's own file lock
        and fsync — terrible throughput, possible deadlock if a future
        EventBus consumer ever calls back into FaultIsolator. Queue
        here while holding the lock; flush in ``_drain_pending_events``
        after release."""
        self._pending_events.append((event_type, dict(payload)))

    def _drain_pending_events(self) -> None:
        """Emit queued audit events. MUST be called outside ``self._lock``.

        Swallows per-event OSErrors (file lock contention, disk full) so
        a downed EventBus cannot freeze the fault path. Wider exceptions
        (M-6 fix) are NOT swallowed - those are programmer bugs that
        should surface, not crash audit silently.

        P0-#3 fix: snapshot + clear must happen atomically under
        ``self._lock``, otherwise a concurrent recording thread that
        appends between ``list(self._pending_events)`` and
        ``.clear()`` has its event silently dropped. Original code
        had a comment claiming the lock was taken; the code did not
        actually take it.
        """
        with self._lock:
            if self._event_bus is None:
                self._pending_events.clear()
                return
            pending = list(self._pending_events)
            self._pending_events.clear()
        # Emit OUTSIDE the lock per H-1 (don't hold our lock across the
        # EventBus's file lock + fsync).
        for event_type, payload in pending:
            try:
                self._event_bus.emit(event_type, payload)
            except (OSError, RuntimeError) as exc:
                # M-6: scope of swallow is I/O errors (OSError) AND
                # generic runtime failures from the bus (RuntimeError),
                # but NOT programmer errors (TypeError, AttributeError,
                # NameError) which should propagate so bugs surface.
                logger.warning("fault_isolation EventBus emit failed: %s", exc)

    def flush(self) -> None:
        """Force-persist any dirty in-memory state. Useful at shutdown
        when H-10's lazy persistence might otherwise lose recent stats."""
        with self._lock:
            if self._dirty:
                self._persist()
        self._drain_pending_events()

    def replay_from_event_bus(self) -> int:
        """P3: rebuild in-memory failure deques from signed
        ``failure.observed`` events on the bus.

        Called at startup so an attacker who crashes the process to
        evade the threshold counter has the counter reconstructed from
        the audit stream rather than reset to zero. Returns the number
        of failure events replayed. Safe to call multiple times - it
        clears the in-memory deques first to avoid double-counting.

        Production-mode-only: if no event_bus is configured this is a
        no-op (the audit feed isn't there to read).
        """
        if self._event_bus is None:
            return 0
        with self._lock:
            self._ensure_loaded()
            # Clear in-memory failures first so a re-call doesn't
            # double-count. Persisted health (transition counters,
            # circuit states) remains from disk - those came from
            # _persist on transitions and are authoritative.
            self._failures.clear()
            cutoff_ts = (
                datetime.now(timezone.utc).timestamp() - self._failure_window
            )
            count = 0
            for event in self._event_bus.replay(event_types=["failure.observed"]):
                payload = event.payload or {}
                aid = payload.get("agent_id", "")
                ts_str = payload.get("timestamp", "")
                if not aid or not ts_str:
                    continue
                try:
                    ts_epoch = datetime.fromisoformat(ts_str).timestamp()
                except (ValueError, TypeError):
                    continue
                if ts_epoch < cutoff_ts:
                    continue   # outside the failure window
                self._failures.setdefault(
                    aid, deque(maxlen=self._failures_cap),
                ).append(
                    FailureRecord(
                        agent_id=aid,
                        action_type=str(payload.get("action_type", "")),
                        error=str(payload.get("error", ""))[:500],
                        timestamp=ts_str,
                    )
                )
                count += 1
            return count

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
