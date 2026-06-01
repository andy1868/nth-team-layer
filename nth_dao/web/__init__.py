"""Unified local web console for NTH DAO.

The web layer is intentionally thin: it exposes the existing local-first
membership and group APIs without bypassing their permission checks.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nth_dao.discovery import AgentRegistry, LANDiscovery, PeerFinder
from nth_dao.groups import DEFAULT_CHANNEL_ID, GroupManager, TaskStatus
from nth_dao.group_registry import (
    GroupRegistry,
    GroupRegistryError,
    PolicyChangeProposal,
    cast_vote as gr_cast_vote,
    resolve_proposal,
)
from nth_dao.membership import MembershipManager, TeamConfig, TeamRole
from nth_dao.orchestration import MissionStore
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
    def summary() -> dict[str, Any]:
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
        }

    @app.get("/api/state")
    def dao_state(agent_id: str = DEFAULT_ADMIN_ID, channel_id: str = DEFAULT_CHANNEL_ID) -> dict[str, Any]:
        _require_member_or_joinable(state, agent_id)
        config = state.membership.load_config()
        return {
            "team": _team_dict(config),
            "actor": {"agent_id": agent_id, "role": config.role_for(agent_id).value},
            "members": _members(state, config),
            "channels": [c.to_dict() for c in state.groups.list_channels(actor_id=agent_id)],
            "messages": [m.to_dict() for m in state.groups.list_messages(channel_id, actor_id=agent_id, limit=100)],
            "announcements": [a.to_dict() for a in state.groups.list_announcements(channel_id)],
            "tasks": [t.to_dict() for t in state.groups.list_tasks()],
            "audit": [e.to_dict() for e in state.groups.list_audit_events(limit=50)],
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
        return msg.to_dict()

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

    # ─── v0.9.6: agent search + LAN discovery + add-friend ───

    @app.get("/api/agents/search")
    def search_agents(q: str = "", limit: int = 10) -> dict[str, Any]:
        """QQ/WeChat-style fuzzy search across registered agents.

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
        """Active "people nearby" — UDP broadcast on the LAN.

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
            target_id = target_id or f"did-{pubkey_hex[:12]}"
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

    # ─── v0.9.6: group registry CRUD + search ───

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
            import uuid as _uuid
            record.group_id = _uuid.uuid4().hex[:12]
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

    # ─── v0.9.6: group governance via signed votes ───

    @app.post("/api/groups/registry/{group_id}/proposals")
    def create_proposal(group_id: str, payload: PolicyProposalPayload) -> dict[str, Any]:
        """Build an unsigned policy-change proposal for the caller (TS) to sign."""
        group = state.group_registry.load_by_id(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        if payload.actor_pubkey_hex not in group.member_pubkeys:
            raise HTTPException(status_code=403, detail="only members can propose")
        # Build an unsigned skeleton — TS signs and posts via /publish below.
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
        """Append a pre-signed vote to a proposal.

        The vote sig is over `canonical_json({"proposal_id", "choice", "voted_at"})`
        — TS must pre-sign and pass the full payload.
        """
        proposal = state.group_registry.load_proposal(proposal_id)
        if proposal is None or proposal.group_id != group_id:
            raise HTTPException(status_code=404, detail="proposal not found")
        if payload.choice not in ("yes", "no", "abstain"):
            raise HTTPException(status_code=400, detail="choice must be yes/no/abstain")
        # The caller passes voter_pubkey_hex + the sig field implicitly via
        # a fully-formed vote dict in `proposal.votes` would normally be cleaner;
        # but we accept just the choice here and require the proposer/voter
        # to call /publish for full-flow control. This endpoint is a convenience
        # that records WHO claims to have voted; verification still happens at
        # resolve time.
        voted_at = datetime.now().isoformat()
        proposal.votes.append({
            "voter_pubkey": payload.voter_pubkey_hex,
            "choice": payload.choice,
            "voted_at": voted_at,
            "sig": "",  # will be filled by /publish once UI gets a sig
        })
        state.group_registry.save_proposal(proposal)
        return {"recorded": True, "proposal": proposal.to_dict()}

    @app.post("/api/groups/registry/{group_id}/proposals/{proposal_id}/sign_vote")
    def add_signed_vote(group_id: str, proposal_id: str, payload: SignedVotePayload) -> dict[str, Any]:
        proposal = state.group_registry.load_proposal(proposal_id)
        if proposal is None or proposal.group_id != group_id:
            raise HTTPException(status_code=404, detail="proposal not found")
        proposal.votes.append(payload.vote)
        state.group_registry.save_proposal(proposal)
        group = state.group_registry.load_by_id(group_id)
        passed, reason = resolve_proposal(proposal, group) if group else (False, "no group")
        return {
            "proposal": proposal.to_dict(),
            "resolved": {"passed": passed, "reason": reason},
        }

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


def _require_member_or_joinable(state: WebState, agent_id: str) -> None:
    config = state.membership.load_config()
    if config.role_for(agent_id) != TeamRole.GUEST:
        return
    ok, reason = state.membership.ensure_member(agent_id)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)


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
        }
        for agent_id in sorted(config.member_ids)
    ]


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
