"""Unified local web console for NTH DAO.

The web layer is intentionally thin: it exposes the existing local-first
membership and group APIs without bypassing their permission checks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("nth_dao.web")

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nth_dao.agent_code import code_for_agent_id, code_for_pubkey, parse_code
from nth_dao.demo_responder import DEFAULT_AGENT_ID as ECHO_AGENT_ID
from nth_dao.demo_responder import maybe_reply as _demo_maybe_reply
from nth_dao.discovery import AgentRegistry, LANDiscovery, PeerFinder
from nth_dao.groups import DEFAULT_CHANNEL_ID, GroupManager, TaskStatus
from nth_dao.group_registry import (
    GroupRegistry,
    GroupRegistryError,
    PolicyChangeProposal,
    cast_vote as gr_cast_vote,
    resolve_proposal,
)
from nth_dao.identity import AgentID
from nth_dao.mandate import (
    KIND_CART,
    KIND_INTENT,
    KIND_PAYMENT,
    KINDS as MANDATE_KINDS,
    MandateStore,
    cart_mandate_digest,
    cart_satisfies_intent,
    complete_triad_chain,
    intent_mandate_digest,
    is_cart_expired,
    is_intent_expired,
    is_payment_expired,
    payment_mandate_digest,
    verify_cart_mandate,
    verify_intent_mandate,
    verify_payment_mandate,
)
from nth_dao.membership import MembershipManager, TeamConfig, TeamRole
from nth_dao.orchestration import MissionStore
from nth_dao.web.rate_limit import RateLimiter, enforce_min_response_time
from team_layer.blackboard import Blackboard


DEFAULT_ADMIN_ID = "admin"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebState:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.membership = MembershipManager(workspace)
        self.groups = GroupManager(workspace, membership=self.membership)
        self.registry = AgentRegistry(str(workspace / "team_agents"))
        self.missions = MissionStore(str(workspace / "missions"))
        self.blackboard = Blackboard(workspace / "blackboard")
        # v0.9.6: cross-workspace-unique group registry + governance
        self.group_registry = GroupRegistry(workspace)
        self.peer_finder = PeerFinder(self.registry)
        # v0.10 T-9: Mandate triad file-backed store, sidebar reads from this
        self.mandates = MandateStore(workspace)
        # v0.10 V-30: per-actor rate limiters for the two crypto-heavy
        # /api/mandates/* routes. The verify endpoint is more sensitive
        # (free oracle + timing side-channel) so its window is tighter.
        # Defaults sized for a sidebar in active use: ~30 verify calls
        # per minute is plenty for clicking through a panel of rows.
        self.verify_limiter = RateLimiter(
            max_per_window=30, window_seconds=60.0,
        )
        self.store_limiter = RateLimiter(
            max_per_window=60, window_seconds=60.0,
        )


class JoinPayload(BaseModel):
    agent_id: str
    token: str = ""


class ChannelPayload(BaseModel):
    actor_id: str
    name: str
    topic: str = ""
    channel_id: str = ""
    is_private: bool = False
    member_ids: list[str] = []


class MessagePayload(BaseModel):
    agent_id: str
    body: str
    channel_id: str = DEFAULT_CHANNEL_ID


class AnnouncementPayload(BaseModel):
    author_id: str
    title: str
    body: str
    channel_id: str = DEFAULT_CHANNEL_ID


class TaskPayload(BaseModel):
    created_by: str
    title: str
    description: str = ""
    assignee_id: str = ""
    channel_id: str = DEFAULT_CHANNEL_ID
    due_at: str = ""


class TaskStatusPayload(BaseModel):
    actor_id: str
    status: str
    note: str = ""


# v0.9.6: add-friend / search / discover / group-governance payloads


class AddAgentPayload(BaseModel):
    """Friend-request style direct add. Resolves an agent_id OR a did:key."""
    actor_id: str
    target_agent_id: str = ""
    target_did: str = ""
    label: str = ""


class GroupCreatePayload(BaseModel):
    actor_id: str
    actor_pubkey_hex: str           # signing pubkey of the founder
    display_name: str
    description: str = ""
    policy: str = "open"            # open | approval | closed | voted


class GroupSearchPayload(BaseModel):
    query: str
    limit: int = 10
    policy: Optional[str] = None


class PolicyProposalPayload(BaseModel):
    actor_pubkey_hex: str
    group_id: str
    new_policy: Optional[str] = None
    add_member_pubkeys: list[str] = []
    remove_member_pubkeys: list[str] = []
    new_display_name: Optional[str] = None
    rationale: str = ""
    ttl_days: int = 7


class VoteCastPayload(BaseModel):
    voter_pubkey_hex: str
    proposal_id: str
    choice: str = "yes"   # yes / no / abstain


class LANDiscoverPayload(BaseModel):
    timeout_seconds: float = 2.0
    psk: str = ""
    wanted_capabilities: list[str] = []


class GroupPublishPayload(BaseModel):
    record: dict[str, Any]


class ProposalPublishPayload(BaseModel):
    proposal: dict[str, Any]


class SignedVotePayload(BaseModel):
    vote: dict[str, Any]


# v0.10 T-9: Mandate sidebar


class MandateStorePayload(BaseModel):
    """Persist a signed mandate body into the workspace store.

    The sidebar issues this after the browser wallet has signed an
    IntentMandate; settlement adapters issue this after receiving carts
    or completing payments. Server determines digest from the body so
    callers cannot forge an inconsistent index entry.

    Voss V-28: ``actor_id`` is required so the request goes through
    the same membership gate as the rest of the web console.
    """

    kind: str                    # "intent" | "cart" | "payment"
    mandate: dict[str, Any]
    actor_id: str


class MandateVerifyPayload(BaseModel):
    """Verify a mandate's Ed25519 signature against its canonical JSON.

    For carts, optionally bind-check against an intent by passing
    ``against_intent``. For payments, ``against_intent`` and
    ``against_cart`` are both required because a PaymentMandate is only
    authorizing inside the full Intent -> Cart -> Payment triad.

    Voss V-28: ``actor_id`` required for membership gating.
    """

    kind: str                    # "intent" | "cart" | "payment"
    mandate: dict[str, Any]
    against_intent: Optional[dict[str, Any]] = None
    against_cart: Optional[dict[str, Any]] = None
    actor_id: str


def create_app(workspace: str | Path | None = None) -> FastAPI:
    root = Path(workspace or os.environ.get("NTH_WORKSPACE", ".")).resolve()
    state = WebState(root)
    _bootstrap(state)

    app = FastAPI(
        title="NTH DAO Console",
        description="Local-first web console for NTH DAO membership, groups, tasks, and audit.",
        version="0.9.0",
    )
    app.state.nth = state

    @app.get("/api/summary")
    def summary(actor_id: str = DEFAULT_ADMIN_ID) -> dict[str, Any]:
        config = state.membership.load_config()
        return {
            "team": _team_dict(config),
            "workspace": str(state.workspace),
            "members": len(config.member_ids),
            "channels": len(state.groups.list_channels(actor_id=DEFAULT_ADMIN_ID)),
            "tasks": len(state.groups.list_tasks()),
            "online_agents": len(state.registry.list_alive()),
            "active_missions": len(state.missions.list_active()),
            "blackboard_entries": len(state.blackboard.list()),
            "server_time": datetime.now().isoformat(),
            # v0.9.8: surface the caller's stable visible code so the UI
            # can show "Your code: a3f7-b2e8" in the header without an
            # extra round-trip.
            "actor_code": code_for_agent_id(actor_id),
        }

    @app.get("/api/state")
    def dao_state(agent_id: str = DEFAULT_ADMIN_ID, channel_id: str = DEFAULT_CHANNEL_ID) -> dict[str, Any]:
        _require_member_or_joinable(state, agent_id)
        config = state.membership.load_config()
        return {
            "team": _team_dict(config),
            "actor": _actor_dict(agent_id, config.role_for(agent_id).value),
            "members": _members(state, config),
            "channels": [c.to_dict() for c in state.groups.list_channels(actor_id=agent_id)],
            "messages": [m.to_dict() for m in state.groups.list_messages(channel_id, actor_id=agent_id, limit=100)],
            "announcements": [a.to_dict() for a in state.groups.list_announcements(channel_id)],
            "tasks": [t.to_dict() for t in state.groups.list_tasks()],
            "audit": [e.to_dict() for e in state.groups.list_audit_events(limit=50)],
        }

    # v0.9.7: multi-DAO sidebar — one agent can hold many DAOs (home + groups).
    @app.get("/api/daos")
    def list_my_daos(actor_pubkey_hex: str = "", actor_id: str = DEFAULT_ADMIN_ID) -> dict[str, Any]:
        return {"daos": _list_my_daos(state, actor_pubkey_hex, actor_id)}

    @app.post("/api/daos/{slug}/channels")
    def dao_create_channel(slug: str, payload: ChannelPayload) -> dict[str, Any]:
        """Create a channel scoped to a DAO; channel_id auto-prefixed for groups."""
        kind, _ = _resolve_dao(state, slug)
        _require_admin(state, payload.actor_id)
        prefix = _dao_channel_prefix(slug if kind == "group" else "")
        bare_id = payload.channel_id or payload.name or DEFAULT_CHANNEL_ID
        scoped_id = bare_id if bare_id.startswith(prefix) else f"{prefix}{bare_id}"
        channel = state.groups.create_channel(
            payload.name,
            created_by=payload.actor_id,
            topic=payload.topic,
            channel_id=scoped_id,
            is_private=payload.is_private,
            member_ids=payload.member_ids,
        )
        return channel.to_dict()

    @app.post("/api/daos/{slug}/messages")
    def dao_post_message(slug: str, payload: MessagePayload) -> dict[str, Any]:
        kind, record = _resolve_dao(state, slug)
        _require_member(state, payload.agent_id)
        prefix = _dao_channel_prefix(slug if kind == "group" else "")
        channel_id = payload.channel_id or (prefix + "general" if prefix else DEFAULT_CHANNEL_ID)
        if prefix and not channel_id.startswith(prefix):
            raise HTTPException(status_code=400, detail=f"channel_id must start with '{prefix}' for DAO '{slug}'")
        msg = state.groups.post_message(channel_id, sender_id=payload.agent_id, body=payload.body)
        # v0.9.8: fire the responder for this DAO too. Policy / description
        # come from the GroupRecord when present so opt-in heuristics
        # ("demo" in name, "open" policy) work per DAO.
        dao_policy = ""
        dao_description = ""
        if record is not None:
            dao_policy = record.policy.value if hasattr(record.policy, "value") else str(record.policy)
            dao_description = getattr(record, "description", "")
        else:
            dao_policy = state.membership.load_config().join_policy
        reply = _demo_maybe_reply(
            state.groups,
            dao_slug=slug,
            channel_id=channel_id,
            sender_id=payload.agent_id,
            body=payload.body,
            dao_policy=dao_policy,
            dao_description=dao_description,
        )
        result = msg.to_dict()
        if reply:
            result["echo_reply"] = reply
        return result

    @app.get("/api/daos/{slug}/state")
    def dao_scoped_state(
        slug: str,
        agent_id: str = DEFAULT_ADMIN_ID,
        channel_id: str = "",
    ) -> dict[str, Any]:
        kind, record = _resolve_dao(state, slug)
        # Default channel per DAO: legacy `general` for home, `dao-<slug>-general` for groups.
        prefix = _dao_channel_prefix(slug if kind == "group" else "")
        effective_channel = channel_id or (prefix + "general" if prefix else DEFAULT_CHANNEL_ID)

        _require_member_or_joinable(state, agent_id)
        config = state.membership.load_config()
        all_channels = state.groups.list_channels(actor_id=agent_id)
        scoped_channels = [
            c for c in all_channels if _dao_owns_channel(slug if kind == "group" else "", c.channel_id)
        ]
        scoped_announcements = [
            a for a in state.groups.list_announcements()
            if _dao_owns_channel(slug if kind == "group" else "", a.channel_id)
        ]
        scoped_tasks = [
            t for t in state.groups.list_tasks()
            if _dao_owns_channel(slug if kind == "group" else "", t.channel_id)
        ]
        # Members: home → workspace membership; group → pubkey set from GroupRecord.
        if kind == "home":
            members = _members(state, config)
        else:
            members = _members_from_group(record)  # type: ignore[arg-type]
        dao_meta = _dao_meta_dict(slug, kind, record, member_count=len(members))
        return {
            "team": _team_dict(config),
            "actor": _actor_dict(agent_id, config.role_for(agent_id).value),
            "dao": dao_meta,
            "members": members,
            "channels": [c.to_dict() for c in scoped_channels],
            "messages": [
                m.to_dict() for m in state.groups.list_messages(
                    effective_channel, actor_id=agent_id, limit=100,
                )
            ] if scoped_channels or kind == "home" else [],
            "announcements": [a.to_dict() for a in scoped_announcements],
            "tasks": [t.to_dict() for t in scoped_tasks],
            "audit": [e.to_dict() for e in state.groups.list_audit_events(limit=50)],
            "active_channel_id": effective_channel,
        }

    @app.post("/api/join")
    def join(payload: JoinPayload) -> dict[str, Any]:
        ok, reason = state.membership.ensure_member(payload.agent_id, token=payload.token)
        if not ok:
            raise HTTPException(status_code=403, detail=reason)
        return {"ok": True, "reason": reason, "agent_id": payload.agent_id}

    @app.post("/api/channels")
    def create_channel(payload: ChannelPayload) -> dict[str, Any]:
        _require_admin(state, payload.actor_id)
        channel = state.groups.create_channel(
            payload.name,
            created_by=payload.actor_id,
            topic=payload.topic,
            channel_id=payload.channel_id,
            is_private=payload.is_private,
            member_ids=payload.member_ids,
        )
        return channel.to_dict()

    @app.post("/api/messages")
    def post_message(payload: MessagePayload) -> dict[str, Any]:
        _require_member(state, payload.agent_id)
        msg = state.groups.post_message(
            payload.channel_id,
            sender_id=payload.agent_id,
            body=payload.body,
        )
        # v0.9.8: fire the demo responder so the home DAO is conversational
        # out of the box. Skipped silently when the DAO opts out.
        reply = _demo_maybe_reply(
            state.groups,
            dao_slug=HOME_DAO_SLUG,
            channel_id=payload.channel_id,
            sender_id=payload.agent_id,
            body=payload.body,
            dao_policy=state.membership.load_config().join_policy,
        )
        result = msg.to_dict()
        if reply:
            result["echo_reply"] = reply
        return result

    @app.post("/api/announcements")
    def post_announcement(payload: AnnouncementPayload) -> dict[str, Any]:
        _require_permission(state, payload.author_id, "post_announcements")
        ann = state.groups.post_announcement(
            payload.title,
            payload.body,
            author_id=payload.author_id,
            channel_id=payload.channel_id,
        )
        return ann.to_dict()

    @app.post("/api/tasks")
    def create_task(payload: TaskPayload) -> dict[str, Any]:
        _require_member(state, payload.created_by)
        if payload.assignee_id:
            _require_member(state, payload.assignee_id)
        task = state.groups.create_task(
            payload.title,
            created_by=payload.created_by,
            description=payload.description,
            assignee_id=payload.assignee_id,
            channel_id=payload.channel_id,
            due_at=payload.due_at,
        )
        return task.to_dict()

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, payload: TaskStatusPayload) -> dict[str, Any]:
        _require_member(state, payload.actor_id)
        try:
            TaskStatus(payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid task status: {payload.status}") from exc
        try:
            task = state.groups.update_task_status(
                task_id,
                payload.status,
                actor_id=payload.actor_id,
                note=payload.note,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return task.to_dict()

    # v0.9.6: agent search + LAN discovery + add-friend

    @app.get("/api/agents/by_code/{code}")
    def lookup_agent_by_code(code: str) -> dict[str, Any]:
        """Direct code lookup — the 'add by Telegram username' analogue.

        Searches both home-workspace members (code derived from agent_id)
        and every GroupRegistry record's pubkey set (code derived from
        pubkey). Returns the first match; 404 if none.
        """
        try:
            normalized = parse_code(code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 1) Try home members.
        config = state.membership.load_config()
        for agent_id in config.member_ids:
            if code_for_agent_id(agent_id).replace("-", "") == normalized:
                return {
                    "code": code_for_agent_id(agent_id),
                    "agent_id": agent_id,
                    "pubkey_hex": "",
                    "source": "home",
                    "role": config.role_for(agent_id).value,
                }
        # 2) Try every group's pubkey set.
        for record in state.group_registry.list_all():
            for pk in set(record.member_pubkeys + record.admin_pubkeys):
                if code_for_pubkey(pk).replace("-", "") == normalized:
                    return {
                        "code": code_for_pubkey(pk),
                        "agent_id": pk[:16],
                        "pubkey_hex": pk,
                        "source": "group",
                        "group_slug": record.slug,
                        "role": "admin" if pk in record.admin_pubkeys else "member",
                    }
        raise HTTPException(status_code=404, detail=f"agent code '{code}' not found")

    @app.get("/api/agents/search")
    def search_agents(q: str = "", limit: int = 10) -> dict[str, Any]:
        """consumer chat app-inspired fuzzy search across registered agents.

        Matches against agent_id, label (from registry metadata), capabilities
        and groups; returns ranked MatchResults as plain dicts.
        """
        if not q.strip():
            return {"query": q, "results": []}
        results = state.peer_finder.search(q, limit=limit)
        return {
            "query": q,
            "results": [
                {
                    "agent_id": r.record.agent_id,
                    "score": r.score,
                    "status": r.record.status,
                    "hostname": r.record.hostname,
                    "backend_id": r.record.backend_id,
                    "capabilities": list(r.record.capabilities),
                    "groups": list(r.record.groups),
                    "last_seen": r.record.last_seen,
                    "matched": list(r.matched_capabilities),
                }
                for r in results
            ],
        }

    @app.post("/api/agents/lan_discover")
    def lan_discover(payload: LANDiscoverPayload) -> dict[str, Any]:
        """Active "people nearby" via UDP broadcast on the LAN.

        This is server-side initiated: the FastAPI process sends the query
        and collects responses. The TS frontend just consumes the JSON list.
        """
        querier = LANDiscovery(
            agent_id=DEFAULT_ADMIN_ID,
            psk=payload.psk or "",
        )
        peers = querier.discover(
            timeout=min(max(0.5, payload.timeout_seconds), 6.0),
            wanted_capabilities=payload.wanted_capabilities or None,
        )
        return {
            "peers": [
                {
                    "agent_id": p.agent_id,
                    "label": p.label,
                    "capabilities": list(p.capabilities),
                    "groups": list(p.groups),
                    "ws_url": p.ws_url,
                    "pubkey_hex": p.pubkey_hex,
                    "source_addr": p.source_addr,
                    "rtt_ms": p.rtt_ms,
                }
                for p in peers
            ],
        }

    @app.post("/api/agents/add")
    def add_agent(payload: AddAgentPayload) -> dict[str, Any]:
        """Add a known agent as a member of the local team.

        Accepts agent_id (legacy) OR did:key (W3C). Resolution rules:
            - If did, extract the pubkey via decode_ed25519_did_key, derive
              fingerprint-style agent_id.
            - If agent_id given directly, use it as-is.
        Subject to membership policy: the team's join_policy still applies.
        """
        _require_admin(state, payload.actor_id)
        target_id = payload.target_agent_id.strip()
        if payload.target_did:
            from nth_dao.did_key import decode_ed25519_did_key_hex, is_did_key
            if not is_did_key(payload.target_did):
                raise HTTPException(status_code=400, detail="invalid did:key")
            pubkey_hex = decode_ed25519_did_key_hex(payload.target_did)
            target_id = target_id or str(AgentID.from_pubkey(pubkey_hex))
        if not target_id:
            raise HTTPException(status_code=400, detail="target_agent_id or target_did required")
        try:
            ok, reason = state.membership.ensure_member(target_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=403, detail=reason)
        return {
            "ok": True,
            "agent_id": target_id,
            "did": payload.target_did or "",
            "label": payload.label,
        }

    # v0.9.6: group registry CRUD + search

    @app.post("/api/groups/registry")
    def create_unique_group(payload: GroupCreatePayload) -> dict[str, Any]:
        """Create a workspace-unique group. Display name must produce a unique slug."""
        _require_admin(state, payload.actor_id)
        # We can't sign without a private key on the server, so instead we
        # produce the unsigned spec and let the caller pass back a signed
        # record. For the common case we accept a server-side surrogate sign:
        # the founder's pubkey AND signature are echoed back in the response
        # so the TS client can attach them after a wallet signs.
        from nth_dao.group_registry import normalize_group_name, GroupRecord, GroupPolicy
        try:
            slug = normalize_group_name(payload.display_name)
        except GroupRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Reject if slug already taken (without writing anything).
        existing = state.group_registry.load_by_slug(slug)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"slug '{slug}' already taken by group {existing.group_id}",
            )
        try:
            policy = GroupPolicy(payload.policy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown policy {payload.policy!r}") from exc
        # Pre-construct the record; caller (TS) signs and posts back.
        record = GroupRecord(
            group_id="",
            slug=slug,
            display_name=payload.display_name,
            description=payload.description,
            policy=policy,
            founder_pubkey=payload.actor_pubkey_hex,
            member_pubkeys=[payload.actor_pubkey_hex],
            admin_pubkeys=[payload.actor_pubkey_hex],
            signer_pubkey=payload.actor_pubkey_hex,
        )
        return {
            "slug": slug,
            "unsigned_record": record.to_dict(),
            "to_sign": record.signable_dict(),
            "next": "POST /api/groups/registry/publish with proof_id, sig",
        }

    @app.post("/api/groups/registry/publish")
    def publish_group(payload: GroupPublishPayload) -> dict[str, Any]:
        """Persist a signed GroupRecord. Signature must verify; slug must be free."""
        from nth_dao.group_registry import GroupRecord
        try:
            record = GroupRecord.from_dict(payload.record)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid record: {exc}") from exc
        if not record.group_id:
            raise HTTPException(status_code=400, detail="group_id must be signed by the client")
        try:
            state.group_registry.publish(record)
        except GroupRegistryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return record.to_dict()

    @app.get("/api/groups/registry")
    def list_unique_groups() -> dict[str, Any]:
        return {
            "groups": [r.to_dict() for r in state.group_registry.list_all()],
            "index": state.group_registry.load_index(),
        }

    @app.post("/api/groups/registry/search")
    def search_groups(payload: GroupSearchPayload) -> dict[str, Any]:
        from nth_dao.group_registry import GroupPolicy
        policy = None
        if payload.policy:
            try:
                policy = GroupPolicy(payload.policy)
            except ValueError:
                pass
        results = state.group_registry.search(payload.query, limit=payload.limit, policy=policy)
        return {"query": payload.query, "results": [r.to_dict() for r in results]}

    # v0.9.6: group governance via signed votes

    @app.post("/api/groups/registry/{group_id}/proposals")
    def create_proposal(group_id: str, payload: PolicyProposalPayload) -> dict[str, Any]:
        """Build an unsigned policy-change proposal for the caller (TS) to sign."""
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if payload.actor_pubkey_hex not in group.member_pubkeys:
            raise HTTPException(status_code=403, detail="only members can propose")
        # Build an unsigned skeleton. TS signs and posts via /publish below.
        from nth_dao.group_registry import PolicyChangeProposal, GroupPolicy
        from datetime import timedelta
        import uuid as _uuid
        try:
            new_policy = GroupPolicy(payload.new_policy) if payload.new_policy else group.policy
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown policy {payload.new_policy!r}") from exc
        skeleton = PolicyChangeProposal(
            proposal_id=_uuid.uuid4().hex[:12],
            group_id=group.group_id,
            proposer_pubkey=payload.actor_pubkey_hex,
            proposed_policy=new_policy,
            proposed_add_members=list(payload.add_member_pubkeys),
            proposed_remove_members=list(payload.remove_member_pubkeys),
            proposed_display_name=payload.new_display_name,
            rationale=payload.rationale,
            expires_at=(datetime.now() + timedelta(days=max(1, payload.ttl_days))).isoformat(),
        )
        return {
            "unsigned_proposal": skeleton.to_dict(),
            "to_sign": skeleton.signable_dict(),
            "next": "POST /api/groups/registry/{group_id}/proposals/publish with sig",
        }

    @app.post("/api/groups/registry/{group_id}/proposals/publish")
    def publish_proposal(group_id: str, payload: ProposalPublishPayload) -> dict[str, Any]:
        from nth_dao.group_registry import PolicyChangeProposal
        try:
            proposal = PolicyChangeProposal.from_dict(payload.proposal)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid proposal: {exc}") from exc
        if proposal.group_id != group_id:
            raise HTTPException(status_code=400, detail="proposal/group_id mismatch")
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if proposal.proposer_pubkey not in group.member_pubkeys:
            raise HTTPException(status_code=403, detail="only members can propose")
        if not proposal.verify_proposer_signature():
            raise HTTPException(status_code=400, detail="proposer signature invalid")
        state.group_registry.save_proposal(proposal)
        return proposal.to_dict()

    @app.get("/api/groups/registry/{group_id}/proposals")
    def list_proposals(group_id: str) -> dict[str, Any]:
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        proposals = []
        for p in state.group_registry.list_proposals_for(group_id):
            passed, reason = resolve_proposal(p, group)
            d = p.to_dict()
            d["resolved"] = {"passed": passed, "reason": reason}
            proposals.append(d)
        return {"group_id": group_id, "proposals": proposals}

    @app.post("/api/groups/registry/{group_id}/proposals/{proposal_id}/vote")
    def add_vote(group_id: str, proposal_id: str, payload: VoteCastPayload) -> dict[str, Any]:
        """Build an unsigned vote payload for the client to sign."""
        proposal = state.group_registry.load_proposal(proposal_id)
        if proposal is None or proposal.group_id != group_id:
            raise HTTPException(status_code=404, detail="proposal not found")
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if payload.voter_pubkey_hex not in group.member_pubkeys:
            raise HTTPException(status_code=403, detail="only members can vote")
        if payload.choice not in ("yes", "no", "abstain"):
            raise HTTPException(status_code=400, detail="choice must be yes/no/abstain")
        voted_at = datetime.now().isoformat()
        unsigned_vote = {
            "voter_pubkey": payload.voter_pubkey_hex,
            "choice": payload.choice,
            "voted_at": voted_at,
            "sig": "",
        }
        return {
            "unsigned_vote": unsigned_vote,
            "to_sign": {
                "proposal_id": proposal.proposal_id,
                "choice": payload.choice,
                "voted_at": voted_at,
            },
        }

    @app.post("/api/groups/registry/{group_id}/proposals/{proposal_id}/sign_vote")
    def add_signed_vote(group_id: str, proposal_id: str, payload: SignedVotePayload) -> dict[str, Any]:
        proposal = state.group_registry.load_proposal(proposal_id)
        if proposal is None or proposal.group_id != group_id:
            raise HTTPException(status_code=404, detail="proposal not found")
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        ok, reason = proposal.validate_vote(payload.vote, group.member_pubkeys)
        if not ok:
            raise HTTPException(status_code=400, detail=reason)
        voter = payload.vote.get("voter_pubkey", "")
        proposal.votes = [vote for vote in proposal.votes if vote.get("voter_pubkey") != voter]
        proposal.votes.append(payload.vote)
        state.group_registry.save_proposal(proposal)
        passed, reason = resolve_proposal(proposal, group)
        return {
            "proposal": proposal.to_dict(),
            "resolved": {"passed": passed, "reason": reason},
        }

    # v0.10 T-9: Mandate sidebar - read-only listings + verify + store
    #
    # Voss V-28: every mandate route runs through the same membership
    # gate as the rest of the web console. Mandates leak counterparty
    # / amount / settlement-rail metadata; an anonymous reader is not
    # an acceptable default even for local-first deployments.

    @app.get("/api/mandates")
    def list_mandates(actor_id: str) -> dict[str, Any]:
        """List all mandates with summary rows for the sidebar."""
        _require_explicit_actor_id(actor_id)
        _require_member(state, actor_id)
        return {
            "intents": [_summarise_intent(m) for m in state.mandates.list_intents()],
            "carts": [_summarise_cart(m) for m in state.mandates.list_carts()],
            "payments": [
                _summarise_payment(m) for m in state.mandates.list_payments()
            ],
        }

    @app.get("/api/mandates/{kind}/{digest}")
    def get_mandate(
        kind: str, digest: str, actor_id: str,
    ) -> Response:
        """Return the full mandate body for a digest.

        Voss V-48: a mandate body is content-addressed by its digest
        and never changes (re-saving the same digest is a no-op per
        V-36). Serving with ``Cache-Control: public, immutable``
        lets the browser skip the re-fetch entirely on the sidebar's
        next render.
        """
        _require_explicit_actor_id(actor_id)
        _require_member(state, actor_id)
        if kind not in MANDATE_KINDS:
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
        try:
            body = state.mandates.get(kind, digest)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body is None:
            raise HTTPException(status_code=404, detail="mandate not found")
        # F-1 (4th-round audit): "private" not "public" - a mandate
        # body carries counterparty DIDs, amounts, and settlement
        # rail. ``public`` would let shared proxies (corp HTTP
        # proxy, ISP cache, CDN) hold the bytes for 24h, defeating
        # V-28 auth gating entirely. ``private`` means only the end
        # browser's own cache stores it.
        return JSONResponse(
            body,
            headers={
                "Cache-Control": "private, max-age=86400, immutable",
                "ETag": f'"{digest}"',
            },
        )

    @app.post("/api/mandates/store")
    async def store_mandate(payload: MandateStorePayload) -> dict[str, Any]:
        """Persist a signed mandate; returns the canonical digest.

        Server re-derives the digest from the body so the index
        filename is authoritative. Callers cannot pin a wrong digest.

        Shape-checks the body before saving so a junk payload doesn't
        produce a worthless hash file on disk: the W3C VC ``type``
        array must contain the expected mandate type for the kind.

        Voss F-5: store has the same 50ms response-time floor as
        verify, including 403 / 429 / malformed-body paths. Store runs
        signature verification before persistence, so leaving it as a
        fast-fail endpoint recreates the timing oracle that verify
        already closed.
        """
        import time as _time
        _start = _time.monotonic()
        try:
            return await _store_mandate_body(payload, state, _start)
        except HTTPException:
            await enforce_min_response_time(_start, 0.05)
            raise

    async def _store_mandate_body(
        payload: MandateStorePayload,
        state: WebState,
        _start: float,
    ) -> dict[str, Any]:
        _require_explicit_actor_id(payload.actor_id)
        _require_member(state, payload.actor_id)
        # V-30: rate limit the store endpoint too - it runs a full
        # signature verification before persisting (V-29).
        store_decision = state.store_limiter.check(payload.actor_id or "anonymous")
        if not store_decision.allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"store rate limit exceeded; retry after "
                    f"{store_decision.retry_after_seconds:.1f}s"
                ),
                headers={"Retry-After": f"{int(store_decision.retry_after_seconds) + 1}"},
            )
        kind = payload.kind
        if kind not in MANDATE_KINDS:
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
        body = payload.mandate
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="mandate must be a JSON object")
        if not _looks_like_mandate(kind, body):
            raise HTTPException(
                status_code=400,
                detail=f"body does not look like a {kind} mandate "
                "(missing @context / type / credentialSubject)",
            )
        # Voss V-29: refuse to store unsigned / invalidly-signed
        # mandates. Without this gate any client can pollute the
        # sidebar with mandates that no party actually signed.
        try:
            if kind == KIND_INTENT:
                sig_ok, sig_reason = verify_intent_mandate(body)
            elif kind == KIND_CART:
                sig_ok, sig_reason = verify_cart_mandate(body)
            else:
                sig_ok, sig_reason = verify_payment_mandate(body)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"malformed {kind}: {exc}",
            ) from exc
        if not sig_ok:
            raise HTTPException(
                status_code=400,
                detail=f"refusing to store {kind} with invalid signature: "
                f"{sig_reason}",
            )
        try:
            if kind == KIND_INTENT:
                digest = state.mandates.save_intent(body)
            elif kind == KIND_CART:
                digest = state.mandates.save_cart(body)
            else:
                digest = state.mandates.save_payment(body)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid {kind}: {exc}") from exc
        await enforce_min_response_time(_start, 0.05)
        return {"ok": True, "kind": kind, "digest": digest}

    @app.post("/api/mandates/verify")
    async def verify_mandate_route(payload: MandateVerifyPayload) -> dict[str, Any]:
        """Verify signature and (optionally) binding constraints.

        The sidebar's per-row [Verify] button calls this for a quick
        green/red badge; adapters call it before settlement. The
        binding fields (``against_intent`` / ``against_cart``) extend
        the check upward through the triad without forcing a separate
        round-trip per layer.

        Voss V-30 + follow-up timing tightening:

          * Per-actor sliding-window rate limit (30/min by default)
            caps the DoS / oracle exposure.
          * A 50ms response-time floor applies to EVERY return path
            including HTTPException raisings (403 / 429 / 400). The
            outer try/except below catches HTTPException so the
            floor runs before the exception propagates - without
            this, a 403 (non-member) returns in <1ms while a 200
            takes 50ms, leaking membership status via wall-clock.
        """
        import time as _time
        _start = _time.monotonic()
        try:
            return await _verify_mandate_body(payload, state, _start)
        except HTTPException:
            # Pad the error path too so 403 / 429 / 400 don't leak
            # gate identity via latency.
            await enforce_min_response_time(_start, 0.05)
            raise

    async def _verify_mandate_body(
        payload: MandateVerifyPayload,
        state: WebState,
        _start: float,
    ) -> dict[str, Any]:
        _require_explicit_actor_id(payload.actor_id)
        _require_member(state, payload.actor_id)
        decision = state.verify_limiter.check(payload.actor_id or "anonymous")
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"verify rate limit exceeded; retry after "
                    f"{decision.retry_after_seconds:.1f}s"
                ),
                headers={"Retry-After": f"{int(decision.retry_after_seconds) + 1}"},
            )
        kind = payload.kind
        if kind not in MANDATE_KINDS:
            await enforce_min_response_time(_start, 0.05)
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
        body = payload.mandate
        if not isinstance(body, dict):
            await enforce_min_response_time(_start, 0.05)
            raise HTTPException(status_code=400, detail="mandate must be a JSON object")

        # Reject obviously-non-mandate shapes early so the verify
        # tuple's "missing proof" branch doesn't get reported as a
        # signature failure. Without this gate, ``{"junk": True}``
        # would render as a generic signature error which is less
        # useful in the UI than a clear "malformed" badge.
        if not _looks_like_mandate(kind, body):
            await enforce_min_response_time(_start, 0.05)
            return {"ok": False, "reason": f"malformed {kind}: not a W3C VC body"}

        # Layer 1: signature verification.
        # The mandate.verify_*_mandate helpers return (ok, reason)
        # tuples, NOT bare booleans - unpacking them avoids the trap
        # where a truthy tuple gets treated as success.
        try:
            if kind == KIND_INTENT:
                sig_ok, sig_reason = verify_intent_mandate(body)
                expired = is_intent_expired(body)
            elif kind == KIND_CART:
                sig_ok, sig_reason = verify_cart_mandate(body)
                expired = is_cart_expired(body)
            else:
                sig_ok, sig_reason = verify_payment_mandate(body)
                expired = is_payment_expired(body)
        except (ValueError, KeyError, TypeError) as exc:
            await enforce_min_response_time(_start, 0.05)
            return {"ok": False, "reason": f"malformed {kind}: {exc}"}

        if not sig_ok:
            await enforce_min_response_time(_start, 0.05)
            return {
                "ok": False,
                "reason": f"signature verification failed: {sig_reason}",
            }

        checks: list[dict[str, Any]] = [{"name": "signature", "ok": True}]
        if expired:
            checks.append({"name": "expiry", "ok": False, "reason": "expired"})
            await enforce_min_response_time(_start, 0.05)
            return {"ok": False, "reason": "expired", "checks": checks}
        checks.append({"name": "expiry", "ok": True})

        # Layer 2: binding constraints.
        #
        # IntentMandate can be verified standalone. CartMandate may be
        # signature-only for inventory/display, but when an intent is
        # supplied it must satisfy it. PaymentMandate is different: a
        # payment is never settlement-authorizing without the full
        # Intent -> Cart -> Payment chain, so require both bindings.
        if kind == KIND_CART and payload.against_intent is not None:
            ok, reason = cart_satisfies_intent(body, payload.against_intent)
            checks.append({"name": "binds_intent", "ok": ok, "reason": reason})
            if not ok:
                await enforce_min_response_time(_start, 0.05)
                return {"ok": False, "reason": reason, "checks": checks}
        if kind == KIND_PAYMENT:
            if payload.against_cart is None or payload.against_intent is None:
                reason = (
                    "against_intent and against_cart are required when "
                    "verifying payment mandates"
                )
                checks.append(
                    {"name": "complete_triad", "ok": False, "reason": reason}
                )
                await enforce_min_response_time(_start, 0.05)
                return {"ok": False, "reason": reason, "checks": checks}
            ok, reason = complete_triad_chain(
                payload.against_intent, payload.against_cart, body
            )
            checks.append({"name": "complete_triad", "ok": ok, "reason": reason})
            if not ok:
                await enforce_min_response_time(_start, 0.05)
                return {"ok": False, "reason": reason, "checks": checks}

        await enforce_min_response_time(_start, 0.05)
        return {"ok": True, "reason": "", "checks": checks}

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def index():
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse(_frontend_missing_html(), status_code=503)

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def frontend_fallback(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse(
            {"detail": "frontend assets are not built; run npm --prefix frontend run build"},
            status_code=503,
        )

    return app


def _bootstrap(state: WebState) -> None:
    config = state.membership.load_config()
    if not config.admin_ids and not config.member_ids:
        config = state.membership.init_team(
            "NTH DAO",
            policy="open",
            admin_ids=[DEFAULT_ADMIN_ID],
        )
    elif DEFAULT_ADMIN_ID not in config.admin_ids:
        if DEFAULT_ADMIN_ID not in config.member_ids:
            config.member_ids.append(DEFAULT_ADMIN_ID)
        config.admin_ids.append(DEFAULT_ADMIN_ID)
        config.roles[DEFAULT_ADMIN_ID] = TeamRole.OWNER.value
        state.membership.save_config(config)

    if not state.groups.get_channel(DEFAULT_CHANNEL_ID):
        state.groups.create_channel(
            "general",
            created_by=config.admin_ids[0] if config.admin_ids else DEFAULT_ADMIN_ID,
            channel_id=DEFAULT_CHANNEL_ID,
            topic="Default DAO channel",
        )

    # v0.9.8: register the demo responder as a workspace member so its
    # auto-replies pass the membership gate. Skipped if it's already in.
    if ECHO_AGENT_ID not in config.member_ids:
        ok, _ = state.membership.ensure_member(ECHO_AGENT_ID)
        if not ok:
            logger.debug("echo-agent join skipped (membership policy)")


def _require_member_or_joinable(state: WebState, agent_id: str) -> None:
    config = state.membership.load_config()
    if config.role_for(agent_id) != TeamRole.GUEST:
        return
    ok, reason = state.membership.ensure_member(agent_id)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)


def _require_explicit_actor_id(actor_id: str) -> None:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(
            status_code=400,
            detail="actor_id is required for mandate routes",
        )


def _require_member(state: WebState, agent_id: str) -> None:
    config = state.membership.load_config()
    if config.role_for(agent_id) == TeamRole.GUEST:
        raise HTTPException(status_code=403, detail=f"agent '{agent_id}' is not a member")


def _require_admin(state: WebState, agent_id: str) -> None:
    _require_permission(state, agent_id, "manage_members")


def _require_permission(state: WebState, agent_id: str, permission: str) -> None:
    if not state.membership.has_permission(agent_id, permission):
        raise HTTPException(status_code=403, detail=f"agent '{agent_id}' lacks permission '{permission}'")


def _team_dict(config: TeamConfig) -> dict[str, Any]:
    data = config.to_dict()
    data["roles"] = dict(sorted(data.get("roles", {}).items()))
    return data


def _members(state: WebState, config: TeamConfig) -> list[dict[str, Any]]:
    online = {r.agent_id for r in state.registry.list_alive()}
    return [
        {
            "agent_id": agent_id,
            "role": config.role_for(agent_id).value,
            "online": agent_id in online,
            "code": code_for_agent_id(agent_id),
        }
        for agent_id in sorted(config.member_ids)
    ]


def _actor_dict(agent_id: str, role: str) -> dict[str, Any]:
    """Standard shape for the 'who am I' block on every state response."""
    return {
        "agent_id": agent_id,
        "role": role,
        "code": code_for_agent_id(agent_id),
    }


# ─── v0.9.7: multi-DAO helpers ────────────────────────────────────────────
#
# An agent participates in one or more DAOs:
#   - "home" — the local workspace team (single global membership). slug="home".
#   - "group" — any GroupRecord from the cross-workspace GroupRegistry where
#     the agent's pubkey is in admin_pubkeys or member_pubkeys.
#
# DAO-scoped channels carry a `dao-<slug>-` prefix on channel_id. The home
# DAO owns everything WITHOUT that prefix (so existing single-DAO installs
# keep working unchanged).

HOME_DAO_SLUG = "home"


def _dao_channel_prefix(slug: str) -> str:
    """`""` for the home DAO; `dao-<slug>-` for registered groups."""
    if not slug or slug == HOME_DAO_SLUG:
        return ""
    return f"dao-{slug}-"


def _dao_owns_channel(slug: str, channel_id: str) -> bool:
    """True if the given channel_id belongs to the slug-scoped DAO.

    Home DAO owns everything that does NOT start with `dao-`. Group DAOs own
    only ids starting with their own `dao-<slug>-` prefix.
    """
    if not slug or slug == HOME_DAO_SLUG:
        return not channel_id.startswith("dao-")
    return channel_id.startswith(_dao_channel_prefix(slug))


def _list_my_daos(state: WebState, actor_pubkey_hex: str, actor_id: str) -> list[dict[str, Any]]:
    """Return [home, *joined_groups, *browsable_groups] for the sidebar.

    When `actor_pubkey_hex` is empty (e.g. wallet still loading), we list
    every group as "joinable" so the sidebar isn't empty — but `joined`
    flags reflect actual membership.
    """
    config = state.membership.load_config()
    daos: list[dict[str, Any]] = []
    home_member_count = len(config.member_ids)
    daos.append({
        "slug": HOME_DAO_SLUG,
        "display_name": config.team_name or "Home Workspace",
        "kind": "home",
        "group_id": "",
        "description": "Local workspace — the team you're directly part of.",
        "policy": config.join_policy,
        "joined": config.role_for(actor_id).value != "guest",
        "member_count": home_member_count,
    })
    actor_pk = (actor_pubkey_hex or "").lower()
    for record in state.group_registry.list_all():
        all_pubkeys = {p.lower() for p in (record.admin_pubkeys + record.member_pubkeys)}
        joined = bool(actor_pk and actor_pk in all_pubkeys)
        daos.append({
            "slug": record.slug,
            "display_name": record.display_name,
            "kind": "group",
            "group_id": record.group_id,
            "description": record.description,
            "policy": record.policy.value if hasattr(record.policy, "value") else str(record.policy),
            "joined": joined,
            "member_count": len(record.member_pubkeys),
            "admin_count": len(record.admin_pubkeys),
        })
    return daos


def _resolve_dao(state: WebState, slug: str) -> tuple[str, Optional[Any]]:
    """Return ("home", None) or ("group", GroupRecord), or 404."""
    if not slug or slug == HOME_DAO_SLUG:
        return ("home", None)
    record = state.group_registry.load_by_slug(slug)
    if record is None:
        # Tolerate group_id lookups too — handy when the slug is unknown to
        # the caller but the group_id was carried over from a search result.
        record = state.group_registry.load_by_id(slug)
    if record is None:
        raise HTTPException(status_code=404, detail=f"DAO '{slug}' not found")
    return ("group", record)


def _members_from_group(record: Any) -> list[dict[str, Any]]:
    """Synthesize a `members` array from a GroupRecord's pubkey set.

    Every member carries a copy-and-paste-able ``code`` derived from
    their pubkey so the UI can show a stable handle instead of the
    raw 64-char hex. We can't tell online/offline from the registry
    alone, so ``online`` is False everywhere — LAN discovery fills
    that in later.
    """
    admin_set = {p.lower() for p in record.admin_pubkeys}
    out: list[dict[str, Any]] = []
    for pk in sorted(set(record.member_pubkeys + record.admin_pubkeys)):
        out.append({
            "agent_id": pk[:16],   # short display id
            "role": "admin" if pk.lower() in admin_set else "member",
            "online": False,
            "pubkey_hex": pk,
            "code": code_for_pubkey(pk),
        })
    return out


def _dao_meta_dict(slug: str, kind: str, record: Any, *, member_count: int) -> dict[str, Any]:
    if kind == "home":
        return {
            "slug": HOME_DAO_SLUG,
            "kind": "home",
            "display_name": "Home Workspace",
            "group_id": "",
            "description": "Local workspace — the team you're directly part of.",
            "policy": "",
            "member_count": member_count,
        }
    return {
        "slug": record.slug,
        "kind": "group",
        "display_name": record.display_name,
        "group_id": record.group_id,
        "description": record.description,
        "policy": record.policy.value if hasattr(record.policy, "value") else str(record.policy),
        "member_count": member_count,
        "admin_count": len(record.admin_pubkeys),
        "founder_pubkey": record.founder_pubkey if hasattr(record, "founder_pubkey") else "",
    }


# v0.10 T-9: cheap shape check for the Mandate routes. We compare
# against the W3C VC ``type`` array set by ``build_*_mandate`` rather
# than parsing the body, so a draft body the wallet has not yet
# signed still passes (the sidebar saves drafts) while obvious junk
# is rejected before it produces a useless digest file on disk.

_EXPECTED_TYPE_TOKEN = {
    KIND_INTENT: "IntentMandate",
    KIND_CART: "CartMandate",
    KIND_PAYMENT: "PaymentMandate",
}


def _looks_like_mandate(kind: str, body: dict[str, Any]) -> bool:
    """True if ``body`` is W3C VC shaped and tagged for the kind.

    The check is intentionally minimal - it must accept any well
    formed mandate the build_*_mandate functions produce, including
    pre-signing drafts (no proof block yet). It must reject:

      * non-dicts and dicts missing the W3C VC backbone,
      * mandates of one kind being saved under another kind's slot.

    Anything stricter belongs in ``verify_*_mandate``.
    """
    if not isinstance(body, dict):
        return False
    if "@context" not in body or "credentialSubject" not in body:
        return False
    expected = _EXPECTED_TYPE_TOKEN.get(kind)
    if expected is None:
        return False
    type_field = body.get("type")
    if isinstance(type_field, str):
        return type_field == expected
    if isinstance(type_field, list):
        return expected in type_field
    return False


# v0.10 T-9: sidebar row summarisers - extract only the fields the
# UI displays, so the JSON over the wire stays small even when carts
# carry rich line-item arrays. Each summariser tolerates missing
# fields (the store may hold a draft mandate the UI saved before
# signing) and falls back to empty strings rather than raising.


def _summarise_intent(mandate: dict[str, Any]) -> dict[str, Any]:
    """Project an IntentMandate to its sidebar row.

    Field map per ``nth_dao.mandate.intent.build_intent_mandate``:

      - top-level ``issuer`` is the DAO did:key
      - top-level ``validUntil`` is the expiry timestamp
      - ``credentialSubject.id`` is the agent_did being authorised
      - ``credentialSubject.purpose`` is the human label
      - constraints sit under ``credentialSubject.constraints.*``
    """
    subject = mandate.get("credentialSubject") or {}
    constraints = subject.get("constraints") or {}
    max_amount = constraints.get("max_amount") or {}
    try:
        digest = intent_mandate_digest(mandate)
    except Exception:  # pragma: no cover - malformed body in store
        digest = ""
    return {
        "kind": KIND_INTENT,
        "digest": digest,
        "issuer": mandate.get("issuer", ""),
        "agent": subject.get("id", ""),
        "purpose": subject.get("purpose", ""),
        "max_amount": {
            "currency": max_amount.get("currency", ""),
            "value": str(max_amount.get("value", "")),
        },
        "expires_at": mandate.get("validUntil", ""),
        "expired": _safe_is_expired(is_intent_expired, mandate),
        "allowed_counterparties": list(
            constraints.get("allowed_counterparties") or []
        ),
        "allowed_settlement_methods": list(
            constraints.get("allowed_settlement_methods") or []
        ),
    }


def _summarise_cart(mandate: dict[str, Any]) -> dict[str, Any]:
    """Project a CartMandate to its sidebar row.

    Field map per ``nth_dao.mandate.cart.build_cart_mandate``:

      - top-level ``issuer`` is the seller did:key
      - top-level ``validUntil`` is the offer-window expiry
      - ``credentialSubject.id`` is the BUYER did (not surfaced -
        the sidebar groups by issuer instead)
      - ``credentialSubject.intent_mandate_digest`` is the binding
      - line items live under ``credentialSubject.items``
    """
    subject = mandate.get("credentialSubject") or {}
    total = subject.get("total") or {}
    try:
        digest = cart_mandate_digest(mandate)
    except Exception:  # pragma: no cover - malformed body in store
        digest = ""
    return {
        "kind": KIND_CART,
        "digest": digest,
        "issuer": mandate.get("issuer", ""),
        "intent_digest": subject.get("intent_mandate_digest", ""),
        "total": {
            "currency": total.get("currency", ""),
            "value": str(total.get("value", "")),
        },
        "settlement_methods": list(subject.get("settlement_methods") or []),
        "expires_at": mandate.get("validUntil", ""),
        "expired": _safe_is_expired(is_cart_expired, mandate),
        "line_item_count": len(subject.get("items") or []),
    }


def _summarise_payment(mandate: dict[str, Any]) -> dict[str, Any]:
    """Project a PaymentMandate to its sidebar row.

    Field map per ``nth_dao.mandate.payment.build_payment_mandate``:

      - top-level ``issuer`` is the DAO authorising settlement
      - top-level ``validUntil`` is the settlement-authority window
      - ``credentialSubject.id`` is the PAYEE did:key
      - ``credentialSubject.cart_mandate_digest`` is the binding
      - ``credentialSubject.settlement_choice`` is the chosen rail
    """
    subject = mandate.get("credentialSubject") or {}
    try:
        digest = payment_mandate_digest(mandate)
    except Exception:  # pragma: no cover - malformed body in store
        digest = ""
    return {
        "kind": KIND_PAYMENT,
        "digest": digest,
        "issuer": mandate.get("issuer", ""),
        "cart_digest": subject.get("cart_mandate_digest", ""),
        "payee": subject.get("id", ""),
        "settlement_choice": subject.get("settlement_choice", ""),
        "issued_at": mandate.get("issuanceDate", ""),
        "expires_at": mandate.get("validUntil", ""),
        "expired": _safe_is_expired(is_payment_expired, mandate),
    }


def _safe_is_expired(checker, mandate: dict[str, Any]) -> bool:
    """Best-effort expiry check; malformed timestamps -> False.

    The store may hold drafts during sidebar editing; surface them as
    not-expired rather than 500-ing the whole listing route.
    """
    try:
        return bool(checker(mandate))
    except Exception:
        return False


def _frontend_missing_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NTH DAO Console</title>
</head>
<body>
  <main>
    <h1>NTH DAO Console</h1>
    <p>Frontend assets are not built. Run <code>npm --prefix frontend run build</code>.</p>
  </main>
</body>
</html>"""


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("NTH_HOST", "127.0.0.1")
    port = int(os.environ.get("NTH_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port)


__all__ = ["app", "create_app", "main"]
