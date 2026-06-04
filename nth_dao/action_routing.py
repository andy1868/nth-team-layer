"""
Action Router — agent-native action dispatch system.

Agents declare what actions they can handle via ``register()``, and the
router dispatches incoming ``ActionRequest`` messages to the matching
handler.  Routing across agents leverages the existing ``TeamChannel``
for message-based dispatch and ``PeerFinder`` for capability-aware
target selection.

Lifecycle
---------

    REQUEST → ACCEPTED → RUNNING → COMPLETED | FAILED | REJECTED

Usage::

    router = ActionRouter(agent_id="alice", channel=team.channel)

    # Register a handler
    router.register(
        action_type="code_review",
        handler=lambda req: {"verdict": "LGTM", "issues": 0},
        description="Review a pull request",
        input_schema={"type": "object", "properties": {"pr_url": {"type": "string"}}},
    )

    # Handle an incoming request
    request = ActionRequest.from_dict(incoming_data)
    response = router.handle(request)

    # Route a request to another agent (via channel)
    router.route("code_review", params={"pr_url": "..."}, target_agent="bob")

    # Discover + route (finds best match via PeerFinder)
    router.dispatch("code_review", params={"pr_url": "..."}, finder=team.finder)

Trust Model
-----------

When an ``AgentIdentity`` with Ed25519 keys is configured, the router
**verifies** incoming request signatures before dispatching to handlers.
Unsigned requests are accepted only when no identity is configured
(dev/local mode).  Outgoing requests and responses are always signed
when keys are available.

Design
------

- Zero external dependencies — pure stdlib.
- Handlers are plain callables ``(ActionRequest) -> Any``.
- ``input_schema`` is JSON Schema (optional, for discovery/docs).
- Handler lookup is O(1) via dict.
- Requests/responses are signed when identity has Ed25519 keys.
- Byte-offset index for O(1) log lookups (same pattern as ``EventBus``).
- Idempotency: repeated request_ids within a bounded window are
  deduplicated (returns cached response).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .util.io import atomic_write_json, safe_load_json

if TYPE_CHECKING:
    from .channel import TeamChannel
    from .identity import AgentIdentity
    from .discovery.peer_finder import PeerFinder, MatchResult, AgentRecord

logger = logging.getLogger("nth_dao.action_routing")

# ────────────────────────── Enums ──────────────────────────


class ActionStatus(str, Enum):
    """Lifecycle states for an action request."""
    REQUEST = "request"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class RouteStrategy(str, Enum):
    """How the router picks a target agent."""
    DIRECT = "direct"          # explicitly named target
    BEST_MATCH = "best_match"  # highest-scoring capable agent
    FANOUT = "fanout"         # send to all capable agents (fire-and-forget)
    ROUND_ROBIN = "round_robin"  # cycle through capable agents


# ────────────────────────── Data types ──────────────────────────


@dataclass
class HandlerInfo:
    """Metadata about a registered action handler."""
    action_type: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionRequest:
    """An action dispatched from one agent to another.

    Serialised as a JSON channel message with ``content_type="action/request"``.
    """
    request_id: str
    action_type: str
    from_agent: str
    to_agent: str
    params: Dict[str, Any] = field(default_factory=dict)
    strategy: str = RouteStrategy.DIRECT.value
    correlation_id: str = ""      # link related requests (e.g. multi-step)
    reply_to: str = ""            # msg_id for threaded reply
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""                 # Ed25519 hex signature

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_type": self.action_type,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "params": self.params,
            "strategy": self.strategy,
            "correlation_id": self.correlation_id,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActionRequest":
        return cls(
            request_id=data.get("request_id", ""),
            action_type=data.get("action_type", ""),
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            params=data.get("params", {}),
            strategy=data.get("strategy", RouteStrategy.DIRECT.value),
            correlation_id=data.get("correlation_id", ""),
            reply_to=data.get("reply_to", ""),
            timestamp=data.get("timestamp", ""),
            sig=data.get("sig", ""),
        )

    @property
    def short_id(self) -> str:
        return self.request_id[:8] if self.request_id else "?"

    def signable_dict(self) -> dict:
        """Return a deterministic dict for signing (excludes ``sig``)."""
        d = self.to_dict()
        d.pop("sig", None)
        return d


@dataclass
class ActionResponse:
    """Result of executing an action request."""
    request_id: str
    action_type: str
    from_agent: str
    to_agent: str
    status: str = ActionStatus.COMPLETED.value
    result: Any = None
    error: str = ""
    elapsed_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_type": self.action_type,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "timestamp": self.timestamp,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActionResponse":
        return cls(
            request_id=data.get("request_id", ""),
            action_type=data.get("action_type", ""),
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            status=data.get("status", ActionStatus.COMPLETED.value),
            result=data.get("result"),
            error=data.get("error", ""),
            elapsed_ms=data.get("elapsed_ms", 0.0),
            timestamp=data.get("timestamp", ""),
            sig=data.get("sig", ""),
        )

    @property
    def ok(self) -> bool:
        return self.status == ActionStatus.COMPLETED.value

    def signable_dict(self) -> dict:
        """Return a deterministic dict for signing (excludes ``sig``)."""
        d = self.to_dict()
        d.pop("sig", None)
        return d


# ────────────────────────── Router ──────────────────────────


class ActionRouter:
    """Per-agent action handler registry and dispatcher.

    Each agent creates one ``ActionRouter`` and registers handlers for the
    action types it can process.  Incoming ``ActionRequest`` messages are
    dispatched to the matching handler, and ``ActionResponse`` messages are
    sent back via the channel.

    Parameters
    ----------
    agent_id : str
        This agent's identifier.
    channel : TeamChannel, optional
        Used for sending responses and routing requests to other agents.
    identity : AgentIdentity, optional
        Ed25519 identity for signing and verifying requests/responses.
        When set, incoming requests with signatures are verified before
        dispatch; unsigned requests are rejected.
    workspace : Path, optional
        Working directory.  Defaults to ``Path.cwd()``.
    storage_dir : str
        Subdirectory under workspace for action logs.  Default ``"team_actions"``.
    max_dedup_entries : int
        Number of recent request_ids to track for idempotency.  Default 1024.
    """

    DEFAULT_STORAGE_DIR = "team_actions"
    DEFAULT_DEDUP_SIZE = 1024

    def __init__(
        self,
        agent_id: str,
        *,
        channel: Optional["TeamChannel"] = None,
        identity: Optional["AgentIdentity"] = None,
        workspace: Optional[Path] = None,
        storage_dir: str = DEFAULT_STORAGE_DIR,
        max_dedup_entries: int = DEFAULT_DEDUP_SIZE,
    ):
        self.agent_id = agent_id
        self._channel = channel
        self._identity = identity
        self._workspace = workspace or Path.cwd()
        self._storage_dir = storage_dir
        self._handlers: Dict[str, Callable[[ActionRequest], Any]] = {}
        self._handler_info: Dict[str, HandlerInfo] = {}
        self._round_robin_index: Dict[str, int] = {}

        # Idempotency: bounded LRU of seen request_ids → cached response
        self._max_dedup = max(1, max_dedup_entries)
        self._seen: OrderedDict[str, ActionResponse] = OrderedDict()

        # Ensure storage directory exists
        self._log_dir = self._workspace / storage_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ── Signature verification ────────────────────────────

    @property
    def _verify_enabled(self) -> bool:
        """Signatures are verified when an identity is configured.

        When no identity is set (dev/local mode), unsigned requests are
        accepted.  This mirrors the trust model used by ``EventBus``.
        """
        return self._identity is not None and self._identity.can_sign

    def _verify_request(self, request: ActionRequest) -> bool:
        """Check that *request.sig* is a valid signature from *request.from_agent*.

        Returns True when:
        - ``_verify_enabled`` is False (no identity configured — dev mode).
        - ``request.sig`` is non-empty and passes verification against the
          signable payload using *from_agent*'s public key.

        Returns False when:
        - ``_verify_enabled`` is True and ``request.sig`` is empty.
        - The signature does not verify.
        """
        if not self._verify_enabled:
            return True  # dev mode — trust all

        if not request.sig:
            return False  # production mode — reject unsigned

        try:
            payload = request.signable_dict()
            assert self._identity is not None  # guarded by _verify_enabled
            return self._identity.verify_json(payload, request.sig)
        except Exception:
            logger.exception("signature verification error for %r", request.short_id)
            return False

    # ── Handler registry ──────────────────────────────────

    def register(
        self,
        action_type: str,
        handler: Callable[[ActionRequest], Any],
        *,
        description: str = "",
        input_schema: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a handler for an action type.

        Parameters
        ----------
        action_type : str
            Unique action name (e.g. ``"code_review"``, ``"deploy"``).
        handler : callable
            Signature: ``(ActionRequest) -> Any``.  The return value
            becomes ``ActionResponse.result``.
        description : str
            Human-readable description for discovery.
        input_schema : dict, optional
            JSON Schema describing expected ``params``.
        metadata : dict, optional
            Arbitrary key-value metadata (e.g. timeout hints, rate limits).
        """
        if action_type in self._handlers:
            logger.warning("action_type %r already registered — overwriting", action_type)

        self._handlers[action_type] = handler
        self._handler_info[action_type] = HandlerInfo(
            action_type=action_type,
            description=description,
            input_schema=input_schema or {},
            metadata=metadata or {},
        )
        logger.debug("registered handler for %r", action_type)

    def unregister(self, action_type: str) -> bool:
        """Remove a handler.  Returns True if it existed."""
        existed = action_type in self._handlers
        self._handlers.pop(action_type, None)
        self._handler_info.pop(action_type, None)
        self._round_robin_index.pop(action_type, None)  # R5: prevent leak
        return existed

    def has_handler(self, action_type: str) -> bool:
        """Check whether this agent can handle *action_type*."""
        return action_type in self._handlers

    @property
    def capabilities(self) -> List[str]:
        """Action types this agent exposes as capabilities.

        These can be merged into ``AgentRecord.capabilities`` so that
        ``PeerFinder`` can route by action type.
        """
        return sorted(self._handlers.keys())

    def list_handlers(self) -> List[HandlerInfo]:
        """Return metadata for every registered handler."""
        return [self._handler_info[t] for t in sorted(self._handlers)]

    def handler_info(self, action_type: str) -> Optional[HandlerInfo]:
        """Return metadata for a single handler, or None."""
        return self._handler_info.get(action_type)

    # ── Execution ─────────────────────────────────────────

    def handle(self, request: ActionRequest) -> ActionResponse:
        """Execute a handler for *request* and return the response.

        This is the core dispatch: verify signature → check idempotency →
        look up handler → invoke → wrap in ``ActionResponse``.

        If no handler is registered, returns a FAILED response.
        If signature verification fails, returns a REJECTED response.
        If the request_id was already processed, returns the cached response.
        """
        # C1: signature verification gate
        if not self._verify_request(request):
            response = ActionResponse(
                request_id=request.request_id,
                action_type=request.action_type,
                from_agent=self.agent_id,
                to_agent=request.from_agent,
                status=ActionStatus.REJECTED.value,
                error="signature verification failed",
            )
            self._log(request, response)
            return response

        # H3: idempotency check
        cached = self._seen.get(request.request_id)
        if cached is not None:
            logger.debug("dedup %r — returning cached response", request.short_id)
            return cached

        start = datetime.now()

        handler = self._handlers.get(request.action_type)
        if handler is None:
            response = ActionResponse(
                request_id=request.request_id,
                action_type=request.action_type,
                from_agent=self.agent_id,
                to_agent=request.from_agent,
                status=ActionStatus.FAILED.value,
                error=f"no handler registered for action_type {request.action_type!r}",
            )
            self._log(request, response)
            self._cache(request.request_id, response)
            return response

        try:
            result = handler(request)
            elapsed = (datetime.now() - start).total_seconds() * 1000
            response = ActionResponse(
                request_id=request.request_id,
                action_type=request.action_type,
                from_agent=self.agent_id,
                to_agent=request.from_agent,
                status=ActionStatus.COMPLETED.value,
                result=result,
                elapsed_ms=round(elapsed, 1),
            )
        except Exception as exc:
            elapsed = (datetime.now() - start).total_seconds() * 1000
            response = ActionResponse(
                request_id=request.request_id,
                action_type=request.action_type,
                from_agent=self.agent_id,
                to_agent=request.from_agent,
                status=ActionStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=round(elapsed, 1),
            )
            logger.exception("handler %r raised", request.action_type)

        self._sign_response(response)
        self._log(request, response)
        self._cache(request.request_id, response)
        return response

    # ── Routing (to other agents) ──────────────────────────

    def route(
        self,
        action_type: str,
        params: Dict[str, Any],
        *,
        target_agent: str,
        strategy: str = RouteStrategy.DIRECT.value,
        correlation_id: str = "",
        reply_to: str = "",
    ) -> Optional[ActionRequest]:
        """Send an action request to a specific agent via the channel.

        Returns the ``ActionRequest`` that was sent, or ``None`` if no
        channel is configured.
        """
        if self._channel is None:
            logger.warning("route() called but no channel configured")
            return None

        request = ActionRequest(
            request_id=uuid.uuid4().hex,
            action_type=action_type,
            from_agent=self.agent_id,
            to_agent=target_agent,
            params=params,
            strategy=strategy,
            correlation_id=correlation_id,
            reply_to=reply_to,
        )

        self._sign_request(request)

        scope = f"dm:{min(self.agent_id, target_agent)}--{max(self.agent_id, target_agent)}"
        self._channel.send(
            content=json.dumps(request.to_dict(), ensure_ascii=False),
            scope=scope,
            content_type="action/request",
            metadata={
                "action_type": action_type,
                "request_id": request.request_id,
                "strategy": strategy,
            },
        )

        self._log_request(request)
        return request

    def dispatch(
        self,
        action_type: str,
        params: Dict[str, Any],
        *,
        finder: Optional["PeerFinder"] = None,
        strategy: str = RouteStrategy.BEST_MATCH.value,
        correlation_id: str = "",
        exclude_agents: Optional[List[str]] = None,
    ) -> Optional[ActionRequest]:
        """Discover a capable agent and route the action to it.

        Uses ``PeerFinder`` to find targets based on *strategy*,
        then calls ``route()``.

        Parameters
        ----------
        finder : PeerFinder
            For capability-based target selection.
        strategy : str
            One of ``RouteStrategy`` values.
        exclude_agents : list, optional
            Agent IDs to skip (e.g. self).
        """
        if finder is None:
            logger.warning("dispatch() called without finder")
            return None

        exclude = list(exclude_agents or [])
        if self.agent_id not in exclude:
            exclude.append(self.agent_id)

        # C3: explicit dispatch per strategy — no fallthrough
        if strategy == RouteStrategy.BEST_MATCH.value:
            return self._dispatch_best_match(
                action_type, params, finder, exclude, correlation_id,
            )
        elif strategy == RouteStrategy.ROUND_ROBIN.value:
            return self._dispatch_round_robin(
                action_type, params, finder, exclude, correlation_id,
            )
        elif strategy == RouteStrategy.FANOUT.value:
            return self._dispatch_fanout(
                action_type, params, finder, exclude, correlation_id,
            )
        else:
            logger.error("dispatch(%r): unknown strategy %r", action_type, strategy)
            return None

    def _dispatch_best_match(
        self,
        action_type: str,
        params: Dict[str, Any],
        finder: "PeerFinder",
        exclude: List[str],
        correlation_id: str,
    ) -> Optional[ActionRequest]:
        # prefer_available is available on PeerFinder v0.9.7+ (feat/agent-capacity).
        # Fall back to basic best_match() for older versions.
        try:
            match = finder.best_match(
                needed_capabilities=[action_type],
                prefer_available=True,
                exclude_agent_ids=exclude,
            )
        except TypeError:
            match = finder.best_match(
                needed_capabilities=[action_type],
                prefer_idle=True,
                exclude_agent_ids=exclude,
            )
        if match is None:
            logger.warning("dispatch(%r): no available agent found", action_type)
            return None
        return self.route(
            action_type, params,
            target_agent=match.record.agent_id,
            strategy=RouteStrategy.BEST_MATCH.value,
            correlation_id=correlation_id,
        )

    def _dispatch_round_robin(
        self,
        action_type: str,
        params: Dict[str, Any],
        finder: "PeerFinder",
        exclude: List[str],
        correlation_id: str,
    ) -> Optional[ActionRequest]:
        # find_available is available on PeerFinder v0.9.7+.
        # Fall back to find() + manual capacity filter for older versions.
        if hasattr(finder, "find_available"):
            available = finder.find_available(
                capability=action_type,
                exclude_agent_ids=exclude,
            )
        else:
            available = finder.find(
                capability=action_type,
                status="idle",
                exclude_agent_ids=exclude,
                only_alive=True,
            )
        if not available:
            logger.warning("dispatch(%r): no available agent for round-robin", action_type)
            return None
        idx = self._round_robin_index.get(action_type, 0) % len(available)
        target = available[idx].agent_id
        self._round_robin_index[action_type] = idx + 1
        return self.route(
            action_type, params,
            target_agent=target,
            strategy=RouteStrategy.ROUND_ROBIN.value,
            correlation_id=correlation_id,
        )

    def _dispatch_fanout(
        self,
        action_type: str,
        params: Dict[str, Any],
        finder: "PeerFinder",
        exclude: List[str],
        correlation_id: str,
    ) -> Optional[ActionRequest]:
        """Fanout: send to all capable agents. Returns the first request sent."""
        if hasattr(finder, "find_available"):
            available = finder.find_available(
                capability=action_type,
                exclude_agent_ids=exclude,
            )
        else:
            available = finder.find(
                capability=action_type,
                status="idle",
                exclude_agent_ids=exclude,
                only_alive=True,
            )
        if not available:
            logger.warning("dispatch(%r): no agents for fanout", action_type)
            return None
        sent = None
        for agent in available:
            req = self.route(
                action_type, params,
                target_agent=agent.agent_id,
                strategy=RouteStrategy.FANOUT.value,
                correlation_id=correlation_id,
            )
            if sent is None:
                sent = req
        return sent

    # ── Inbound processing helpers ────────────────────────

    def parse_incoming(self, message_data: dict) -> Optional[ActionRequest]:
        """Parse a channel message into an ActionRequest (if it is one).

        Returns ``None`` if the message is not an action request (wrong
        content_type or missing fields).
        """
        ct = message_data.get("content_type", "")
        if ct != "action/request":
            return None
        try:
            return ActionRequest.from_dict(message_data)
        except Exception:
            logger.exception("failed to parse action request")
            return None

    def handle_incoming(self, message_data: dict) -> Optional[ActionResponse]:
        """Parse and handle an incoming action request in one call.

        Returns ``None`` if the message is not an action request.
        """
        request = self.parse_incoming(message_data)
        if request is None:
            return None
        return self.handle(request)

    # ── History ───────────────────────────────────────────

    def requests_sent(self, limit: int = 50) -> List[ActionRequest]:
        """Recent action requests sent by this agent."""
        return list(self._read_log("requests_sent", ActionRequest, limit))

    def responses_sent(self, limit: int = 50) -> List[ActionResponse]:
        """Recent action responses sent by this agent."""
        return list(self._read_log("responses_sent", ActionResponse, limit))

    def requests_received(self, limit: int = 50) -> List[ActionRequest]:
        """Recent action requests received by this agent."""
        return list(self._read_log("requests_received", ActionRequest, limit))

    # ── Internals ─────────────────────────────────────────

    def _sign_request(self, request: ActionRequest) -> None:
        if self._identity and self._identity.can_sign:
            request.sig = self._identity.sign_json(request.signable_dict())

    def _sign_response(self, response: ActionResponse) -> None:
        if self._identity and self._identity.can_sign:
            response.sig = self._identity.sign_json(response.signable_dict())

    def _cache(self, request_id: str, response: ActionResponse) -> None:
        """Store response for idempotency; evict oldest when at capacity."""
        self._seen[request_id] = response
        while len(self._seen) > self._max_dedup:
            self._seen.popitem(last=False)

    def _log_request(self, request: ActionRequest) -> None:
        self._append_log("requests_sent", request.to_dict())

    def _log(self, request: ActionRequest, response: ActionResponse) -> None:
        self._append_log("requests_received", request.to_dict())
        self._append_log("responses_sent", response.to_dict())

    # ── Logging with byte-offset index (H1) ───────────────

    def _log_path(self, name: str) -> Path:
        return self._log_dir / f"{self.agent_id}_{name}.jsonl"

    def _index_path(self, name: str) -> Path:
        return self._log_dir / f"{self.agent_id}_{name}.index.json"

    def _append_log(self, name: str, data: dict) -> None:
        """Append one line to the JSONL log and update the byte-offset index."""
        path = self._log_path(name)
        index_path = self._index_path(name)
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.flush()  # ensure file position is accurate before tell
                offset = f.tell()
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
                new_len = len(line.encode("utf-8"))

            # Update request_id → (offset, length) index
            req_id = data.get("request_id", "")
            if req_id:
                index = {}
                if index_path.exists():
                    index = safe_load_json(index_path) or {}
                index[req_id] = [offset, new_len]
                atomic_write_json(index_path, index)
        except OSError:
            logger.exception("failed to write action log %r", name)

    def _read_log(
        self,
        name: str,
        cls: type,
        limit: int,
    ):
        """Read recent entries from a JSONL log.

        Uses the byte-offset index for O(1) lookups when available;
        falls back to a full scan for items not in the index.
        Returns entries in reverse chronological order (most recent first).
        """
        path = self._log_path(name)
        if not path.exists():
            return
        index_path = self._index_path(name)

        # Load index for O(1) reads
        index: Dict[str, list] = {}
        if index_path.exists():
            index = safe_load_json(index_path) or {}

        try:
            with open(path, "rb") as f:
                if index:
                    # Read from index — most recent first
                    indexed_items = sorted(
                        index.items(),
                        key=lambda kv: kv[1][0],  # sort by offset
                        reverse=True,
                    )
                    count = 0
                    for _req_id, (offset, length) in indexed_items:
                        f.seek(offset)
                        raw = f.read(length)
                        try:
                            obj = json.loads(raw.decode("utf-8"))
                            yield cls.from_dict(obj)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        count += 1
                        if count >= limit:
                            return
                else:
                    # No index — full scan fallback
                    f.seek(0)
                    lines = f.read().decode("utf-8").splitlines()
                    count = 0
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield cls.from_dict(json.loads(line))
                        except (json.JSONDecodeError, TypeError):
                            continue
                        count += 1
                        if count >= limit:
                            break
        except OSError:
            logger.exception("failed to read action log %r", name)

    def __repr__(self) -> str:
        n = len(self._handlers)
        return f"ActionRouter(agent={self.agent_id!r}, handlers={n})"
