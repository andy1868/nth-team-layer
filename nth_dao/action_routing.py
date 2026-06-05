"""Action Router — signed cross-agent action dispatch.

Each agent registers handlers for the action types it can serve. An
incoming ``ActionRequest`` is signature-verified against the *claimed*
``from_agent``'s pubkey (looked up via ``pubkey_lookup``), then
dispatched to the matching handler; the resulting ``ActionResponse``
is signed by this agent and returned. Outbound requests are signed
before they leave the local process.

Two-way trust contract
----------------------
The router's security model is "signatures, not trust assertions"
(NTH DAO operating principle P4). To enforce it correctly the router
needs *external* knowledge of who each agent's pubkey is — otherwise
``from_agent = "alice"`` is just a string anyone can write. That
mapping comes from ``pubkey_lookup``, a callable ``str -> str | None``
the integrator wires up. Typical implementations:

    # AgentRegistry-backed
    def lookup(agent_id: str) -> Optional[str]:
        record = registry.get_record(agent_id)
        return record.metadata.get("pubkey_hex") if record else None

    # GroupRegistry-backed for cross-DAO routing
    def lookup(agent_id: str) -> Optional[str]:
        for record in group_registry.list_all():
            for pk in record.member_pubkeys + record.admin_pubkeys:
                if AgentID.from_pubkey(pk).value == agent_id:
                    return pk
        return None

If no ``pubkey_lookup`` is configured the router runs in **dev mode**:
- unsigned requests are accepted
- signed requests are NOT verified (we have no way to)
This is explicit and visible in the constructor, not a hidden default.

Production mode (``pubkey_lookup`` set, identity set, can_sign=True)
rejects:
- requests without a signature
- requests whose claimed ``from_agent`` has no registered pubkey
- requests whose signature does not verify under the claimed pubkey
- (implicitly) requests where an attacker forges ``from_agent="alice"``
  but signs with their own key — the lookup yields alice's pubkey,
  the attacker's signature fails to verify

This is the fix for the critical security bug in the original
@andy1868 submission, which called ``verify_json(payload, sig)``
without passing ``from_agent``'s pubkey — falling back to the
router's own pubkey and accidentally rejecting every legitimate
cross-agent request.

Design contributed by @andy1868 in the agent-collab submission
(June 2026). This implementation preserves the request/response
dataclasses, handler registry, four routing strategies, idempotency
cache, byte-offset indexed JSONL logs, and channel integration,
while rewiring the verification path to correctly check the claimed
sender's pubkey via the new ``pubkey_lookup`` injection point.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .channel import TeamChannel
    from .discovery.peer_finder import PeerFinder
    from .identity import AgentIdentity

logger = logging.getLogger("nth_dao.action_routing")


PubkeyLookup = Callable[[str], Optional[str]]
"""Resolve an agent_id to its hex-encoded Ed25519 pubkey, or None when
the agent is not known. Wired by the integrator to AgentRegistry,
GroupRegistry, or any custom directory."""


# ─────────────────────────── enums ──────────────────────────


class ActionStatus(str, Enum):
    REQUEST = "request"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class RouteStrategy(str, Enum):
    DIRECT = "direct"
    BEST_MATCH = "best_match"
    FANOUT = "fanout"
    ROUND_ROBIN = "round_robin"


# ─────────────────────────── dataclasses ──────────────────────────


@dataclass
class HandlerInfo:
    action_type: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionRequest:
    request_id: str
    action_type: str
    from_agent: str
    to_agent: str
    params: Dict[str, Any] = field(default_factory=dict)
    strategy: str = RouteStrategy.DIRECT.value
    correlation_id: str = ""
    reply_to: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def signable_dict(self) -> dict:
        d = self.to_dict()
        d.pop("sig", None)
        return d

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
        return asdict(self)

    def signable_dict(self) -> dict:
        d = self.to_dict()
        d.pop("sig", None)
        return d

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


# ─────────────────────────── router ──────────────────────────


class ActionRouter:
    """Per-agent action handler registry + signed dispatch.

    Production-mode constructor::

        router = ActionRouter(
            agent_id="alice",
            identity=alice_identity,
            pubkey_lookup=lambda aid: registry.get_record(aid).pubkey_hex,
            channel=team.channel,
            workspace=team.workspace,
        )

    Dev-mode constructor (no verification, no signing)::

        router = ActionRouter(agent_id="alice", workspace=tmp_path)
    """

    DEFAULT_STORAGE_DIR = "team_actions"
    DEFAULT_DEDUP_SIZE = 1024

    def __init__(
        self,
        agent_id: str,
        *,
        identity: Optional["AgentIdentity"] = None,
        pubkey_lookup: Optional[PubkeyLookup] = None,
        channel: Optional["TeamChannel"] = None,
        workspace: Optional[Path] = None,
        storage_dir: str = DEFAULT_STORAGE_DIR,
        max_dedup_entries: int = DEFAULT_DEDUP_SIZE,
    ):
        self.agent_id = agent_id
        self._identity = identity
        self._pubkey_lookup = pubkey_lookup
        self._channel = channel
        self._workspace = workspace or Path.cwd()
        self._handlers: Dict[str, Callable[[ActionRequest], Any]] = {}
        self._handler_info: Dict[str, HandlerInfo] = {}
        self._round_robin_index: Dict[str, int] = {}
        self._max_dedup = max(1, max_dedup_entries)
        self._seen: "OrderedDict[str, ActionResponse]" = OrderedDict()
        self._log_dir = self._workspace / storage_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ── verification (the critical path) ──────────────────

    @property
    def _verify_enabled(self) -> bool:
        """Production mode requires BOTH a local identity (to know who
        we are) AND a pubkey_lookup (to know who the sender is).
        Either alone is insufficient — running with identity-only would
        let an attacker forge from_agent and have us accept it because
        we'd have no way to refute."""
        return (
            self._identity is not None
            and self._identity.can_sign
            and self._pubkey_lookup is not None
        )

    def _verify_request(self, request: ActionRequest) -> bool:
        """Verify *request.sig* using the *claimed* sender's pubkey.

        Dev mode (no identity OR no pubkey_lookup): accept unconditionally.
        Production mode:
          - unsigned request → reject
          - from_agent unknown to lookup → reject
          - signature does not verify under the looked-up pubkey → reject
          - same lookup but attacker signed with their own key → reject
            (because the lookup returns the *claimed* agent's pubkey,
            not the attacker's)
        """
        if not self._verify_enabled:
            return True
        if not request.sig:
            return False
        assert self._pubkey_lookup is not None
        sender_pubkey = self._pubkey_lookup(request.from_agent)
        if not sender_pubkey:
            logger.warning(
                "verify reject %s: from_agent %r not in directory",
                request.short_id, request.from_agent,
            )
            return False
        assert self._identity is not None
        try:
            return self._identity.verify_json(
                request.signable_dict(),
                request.sig,
                pubkey_hex=sender_pubkey,
            )
        except Exception as exc:   # noqa: BLE001
            logger.warning(
                "verify reject %s: signature verification raised: %s",
                request.short_id, exc,
            )
            return False

    # ── handler registry ──────────────────────────────────

    def register(
        self,
        action_type: str,
        handler: Callable[[ActionRequest], Any],
        *,
        description: str = "",
        input_schema: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if action_type in self._handlers:
            logger.warning("action_type %r already registered — overwriting", action_type)
        self._handlers[action_type] = handler
        self._handler_info[action_type] = HandlerInfo(
            action_type=action_type,
            description=description,
            input_schema=input_schema or {},
            metadata=metadata or {},
        )

    def unregister(self, action_type: str) -> bool:
        existed = action_type in self._handlers
        self._handlers.pop(action_type, None)
        self._handler_info.pop(action_type, None)
        self._round_robin_index.pop(action_type, None)
        return existed

    def has_handler(self, action_type: str) -> bool:
        return action_type in self._handlers

    @property
    def capabilities(self) -> List[str]:
        return sorted(self._handlers)

    def list_handlers(self) -> List[HandlerInfo]:
        return [self._handler_info[t] for t in sorted(self._handlers)]

    def handler_info(self, action_type: str) -> Optional[HandlerInfo]:
        return self._handler_info.get(action_type)

    # ── execution ─────────────────────────────────────────

    def handle(self, request: ActionRequest) -> ActionResponse:
        """verify → dedup → dispatch → sign → log."""
        if not self._verify_request(request):
            response = ActionResponse(
                request_id=request.request_id,
                action_type=request.action_type,
                from_agent=self.agent_id,
                to_agent=request.from_agent,
                status=ActionStatus.REJECTED.value,
                error="signature verification failed",
            )
            self._sign_response(response)
            self._log(request, response)
            return response

        cached = self._seen.get(request.request_id)
        if cached is not None:
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
        else:
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
            except Exception as exc:   # noqa: BLE001
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

    # ── outbound routing ──────────────────────────────────

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
        self._append_log("requests_sent", request.to_dict())
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
        if finder is None:
            logger.warning("dispatch() called without finder")
            return None
        exclude = list(exclude_agents or [])
        if self.agent_id not in exclude:
            exclude.append(self.agent_id)

        if strategy == RouteStrategy.BEST_MATCH.value:
            return self._dispatch_best_match(action_type, params, finder, exclude, correlation_id)
        if strategy == RouteStrategy.ROUND_ROBIN.value:
            return self._dispatch_round_robin(action_type, params, finder, exclude, correlation_id)
        if strategy == RouteStrategy.FANOUT.value:
            return self._dispatch_fanout(action_type, params, finder, exclude, correlation_id)
        logger.error("dispatch(%r): unknown strategy %r", action_type, strategy)
        return None

    def _capable_peers(self, finder: "PeerFinder", action_type: str, exclude: List[str]) -> List[Any]:
        if hasattr(finder, "find_available"):
            return finder.find_available(capability=action_type, exclude_agent_ids=exclude)
        return finder.find(
            capability=action_type, status="idle",
            exclude_agent_ids=exclude, only_alive=True,
        )

    def _dispatch_best_match(self, action_type, params, finder, exclude, correlation_id):
        try:
            match = finder.best_match(
                needed_capabilities=[action_type],
                prefer_status="idle",
                exclude_agent_ids=exclude,
            )
        except TypeError:
            match = finder.best_match(
                needed_capabilities=[action_type],
                prefer_idle=True,
                exclude_agent_ids=exclude,
            )
        if match is None:
            logger.warning("dispatch(%r): no available agent", action_type)
            return None
        return self.route(
            action_type, params,
            target_agent=match.record.agent_id,
            strategy=RouteStrategy.BEST_MATCH.value,
            correlation_id=correlation_id,
        )

    def _dispatch_round_robin(self, action_type, params, finder, exclude, correlation_id):
        peers = self._capable_peers(finder, action_type, exclude)
        if not peers:
            return None
        idx = self._round_robin_index.get(action_type, 0) % len(peers)
        target = peers[idx].agent_id
        self._round_robin_index[action_type] = idx + 1
        return self.route(
            action_type, params,
            target_agent=target,
            strategy=RouteStrategy.ROUND_ROBIN.value,
            correlation_id=correlation_id,
        )

    def _dispatch_fanout(self, action_type, params, finder, exclude, correlation_id):
        peers = self._capable_peers(finder, action_type, exclude)
        if not peers:
            return None
        first = None
        for peer in peers:
            sent = self.route(
                action_type, params,
                target_agent=peer.agent_id,
                strategy=RouteStrategy.FANOUT.value,
                correlation_id=correlation_id,
            )
            if first is None:
                first = sent
        return first

    # ── inbound parsing ───────────────────────────────────

    def parse_incoming(self, message_data: dict) -> Optional[ActionRequest]:
        if message_data.get("content_type", "") != "action/request":
            return None
        try:
            return ActionRequest.from_dict(message_data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("failed to parse action request: %s", exc)
            return None

    def handle_incoming(self, message_data: dict) -> Optional[ActionResponse]:
        request = self.parse_incoming(message_data)
        if request is None:
            return None
        return self.handle(request)

    # ── history ───────────────────────────────────────────

    def requests_sent(self, limit: int = 50) -> List[ActionRequest]:
        return list(self._read_log("requests_sent", ActionRequest, limit))

    def responses_sent(self, limit: int = 50) -> List[ActionResponse]:
        return list(self._read_log("responses_sent", ActionResponse, limit))

    def requests_received(self, limit: int = 50) -> List[ActionRequest]:
        return list(self._read_log("requests_received", ActionRequest, limit))

    # ── signing ───────────────────────────────────────────

    def _sign_request(self, request: ActionRequest) -> None:
        if self._identity and self._identity.can_sign:
            request.sig = self._identity.sign_json(request.signable_dict())

    def _sign_response(self, response: ActionResponse) -> None:
        if self._identity and self._identity.can_sign:
            response.sig = self._identity.sign_json(response.signable_dict())

    # ── cache / logging ───────────────────────────────────

    def _cache(self, request_id: str, response: ActionResponse) -> None:
        self._seen[request_id] = response
        while len(self._seen) > self._max_dedup:
            self._seen.popitem(last=False)

    def _log(self, request: ActionRequest, response: ActionResponse) -> None:
        self._append_log("requests_received", request.to_dict())
        self._append_log("responses_sent", response.to_dict())

    def _log_path(self, name: str) -> Path:
        return self._log_dir / f"{self.agent_id}_{name}.jsonl"

    def _append_log(self, name: str, data: dict) -> None:
        try:
            with open(self._log_path(name), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("action log append failed: %s", exc)

    def _read_log(self, name: str, cls, limit: int):
        path = self._log_path(name)
        if not path.exists() or limit <= 0:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("action log read failed: %s", exc)
            return
        for raw in lines[-limit:]:
            line = raw.strip()
            if not line:
                continue
            try:
                yield cls.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue


__all__ = [
    "ActionStatus",
    "RouteStrategy",
    "ActionRequest",
    "ActionResponse",
    "HandlerInfo",
    "ActionRouter",
    "PubkeyLookup",
]
