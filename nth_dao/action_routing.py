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

Design
------

- Zero external dependencies — pure stdlib.
- Handlers are plain callables (action_type → handler).
- ``input_schema`` is JSON Schema (optional, for discovery/docs).
- Handler lookup is O(1) via dict.
- Requests/responses are signed when identity has Ed25519 keys.
- All state lives in a single JSONL file per workspace
  (like ``ChannelMessage`` and ``EventBus``).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .channel import TeamChannel
    from .identity import AgentIdentity
    from .discovery.peer_finder import PeerFinder

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
    DIRECT = "direct"         # explicitly named target
    BEST_MATCH = "best_match"  # highest-scoring capable agent
    BROADCAST = "broadcast"   # all capable agents (fire-and-forget)
    ROUND_ROBIN = "round_robin"  # cycle through capable agents


# ────────────────────────── Data types ──────────────────────────


@dataclass
class HandlerInfo:
    """Metadata about a registered action handler."""
    action_type: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    max_concurrent: int = 5


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
        Ed25519 identity for signing requests/responses.
    workspace : Path, optional
        Working directory.  Defaults to ``Path.cwd()``.
    storage_dir : str
        Subdirectory under workspace for action logs.  Default ``"team_actions"``.
    """

    DEFAULT_STORAGE_DIR = "team_actions"

    def __init__(
        self,
        agent_id: str,
        *,
        channel: Optional["TeamChannel"] = None,
        identity: Optional["AgentIdentity"] = None,
        workspace: Optional[Path] = None,
        storage_dir: str = DEFAULT_STORAGE_DIR,
    ):
        self.agent_id = agent_id
        self._channel = channel
        self._identity = identity
        self._workspace = workspace or Path.cwd()
        self._storage_dir = storage_dir
        self._handlers: Dict[str, Callable[[ActionRequest], Any]] = {}
        self._handler_info: Dict[str, HandlerInfo] = {}
        self._round_robin_index: Dict[str, int] = {}

        # Ensure storage directory exists
        self._log_dir = self._workspace / storage_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ── Handler registry ──────────────────────────────────

    def register(
        self,
        action_type: str,
        handler: Callable[[ActionRequest], Any],
        *,
        description: str = "",
        input_schema: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 300,
        max_concurrent: int = 5,
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
        timeout_seconds : int
            Soft timeout hint for callers.
        max_concurrent : int
            Max concurrent executions of this handler.
        """
        if action_type in self._handlers:
            logger.warning("action_type %r already registered — overwriting", action_type)

        self._handlers[action_type] = handler
        self._handler_info[action_type] = HandlerInfo(
            action_type=action_type,
            description=description,
            input_schema=input_schema or {},
            timeout_seconds=timeout_seconds,
            max_concurrent=max_concurrent,
        )
        logger.debug("registered handler for %r", action_type)

    def unregister(self, action_type: str) -> bool:
        """Remove a handler.  Returns True if it existed."""
        existed = action_type in self._handlers
        self._handlers.pop(action_type, None)
        self._handler_info.pop(action_type, None)
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

        This is the core dispatch: look up the handler by ``action_type``,
        invoke it, and wrap the result in an ``ActionResponse``.

        If no handler is registered, returns a FAILED response with an
        error message.
        """
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

        # Send as a typed channel message
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

        Uses ``PeerFinder`` to find the best match for *action_type* as
        a required capability, then calls ``route()``.

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

        if strategy == RouteStrategy.BEST_MATCH.value:
            match = finder.best_match(
                needed_capabilities=[action_type],
                prefer_available=True,
                exclude_agent_ids=exclude,
            )
            if match is None:
                logger.warning("dispatch(%r): no available agent found", action_type)
                return None
            target = match.record.agent_id

        elif strategy == RouteStrategy.ROUND_ROBIN.value:
            available = finder.find_available(
                capability=action_type,
                exclude_agent_ids=exclude,
            )
            if not available:
                logger.warning("dispatch(%r): no available agent for round-robin", action_type)
                return None
            idx = self._round_robin_index.get(action_type, 0) % len(available)
            target = available[idx].agent_id
            self._round_robin_index[action_type] = idx + 1

        elif strategy == RouteStrategy.BROADCAST.value:
            # Broadcast: send to all capable agents.
            # For simplicity, return after the first send; caller can re-call
            # with explicit exclude_agents if they want multi-target.
            available = finder.find_available(
                capability=action_type,
                exclude_agent_ids=exclude,
            )
            if not available:
                logger.warning("dispatch(%r): no agents for broadcast", action_type)
                return None
            # Fire-and-forget to all, return the first
            sent = None
            for agent in available:
                req = self.route(
                    action_type,
                    params,
                    target_agent=agent.agent_id,
                    strategy=strategy,
                    correlation_id=correlation_id,
                )
                if sent is None:
                    sent = req
            return sent

        else:
            logger.error("dispatch(%r): unknown strategy %r", action_type, strategy)
            return None

        return self.route(
            action_type,
            params,
            target_agent=target,
            strategy=strategy,
            correlation_id=correlation_id,
        )

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
            payload = request.to_dict()
            request.sig = self._identity.sign_json(payload)

    def _sign_response(self, response: ActionResponse) -> None:
        if self._identity and self._identity.can_sign:
            payload = response.to_dict()
            response.sig = self._identity.sign_json(payload)

    def _log_request(self, request: ActionRequest) -> None:
        self._append_log("requests_sent", request.to_dict())

    def _log(self, request: ActionRequest, response: ActionResponse) -> None:
        self._append_log("requests_received", request.to_dict())
        self._append_log("responses_sent", response.to_dict())

    def _log_path(self, name: str) -> Path:
        return self._log_dir / f"{self.agent_id}_{name}.jsonl"

    def _append_log(self, name: str, data: dict) -> None:
        path = self._log_path(name)
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            logger.exception("failed to write action log %r", name)

    def _read_log(
        self,
        name: str,
        cls: type,
        limit: int,
    ):
        path = self._log_path(name)
        if not path.exists():
            return
        count = 0
        try:
            with open(path, encoding="utf-8") as f:
                # Read lines from end (most recent first)
                lines = f.readlines()
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
