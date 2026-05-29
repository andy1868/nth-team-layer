"""Unified local web console for NTH DAO.

The web layer is intentionally thin: it exposes the existing local-first
membership and group APIs without bypassing their permission checks.
"""

from __future__ import annotations

import html
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from nth_dao.discovery import AgentRegistry
from nth_dao.groups import DEFAULT_CHANNEL_ID, GroupManager, TaskStatus
from nth_dao.membership import MembershipManager, TeamConfig, TeamRole
from nth_dao.orchestration import MissionStore
from team_layer.blackboard import Blackboard


DEFAULT_ADMIN_ID = "admin"


class WebState:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.membership = MembershipManager(workspace)
        self.groups = GroupManager(workspace, membership=self.membership)
        self.registry = AgentRegistry(str(workspace / "team_agents"))
        self.missions = MissionStore(str(workspace / "missions"))
        self.blackboard = Blackboard(workspace / "blackboard")


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

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _html()

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

    return app


def _bootstrap(state: WebState) -> None:
    config = state.membership.load_config()
    if not config.admin_ids and not config.member_ids:
        config = state.membership.init_team(
            "NTH DAO",
            policy="open",
            admin_ids=[DEFAULT_ADMIN_ID],
        )
    elif DEFAULT_ADMIN_ID not in config.member_ids:
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


def _html() -> str:
    return html.escape("", quote=False) + """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NTH DAO Console</title>
  <style>
    body { margin: 0; font: 14px/1.45 system-ui, sans-serif; background: #0e1117; color: #e6edf3; }
    header { padding: 14px 18px; border-bottom: 1px solid #30363d; display: flex; gap: 16px; align-items: center; }
    main { display: grid; grid-template-columns: 260px 1fr 300px; min-height: calc(100vh - 57px); }
    section, aside { padding: 14px; border-right: 1px solid #30363d; overflow: auto; }
    aside:last-child { border-right: 0; border-left: 1px solid #30363d; }
    input, textarea, button, select { font: inherit; border-radius: 6px; border: 1px solid #30363d; background: #161b22; color: #e6edf3; padding: 8px; }
    button { cursor: pointer; background: #238636; border-color: #2ea043; }
    textarea { min-height: 64px; resize: vertical; }
    .card { border: 1px solid #30363d; background: #161b22; border-radius: 8px; padding: 10px; margin: 8px 0; }
    .muted { color: #8b949e; }
    .row { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
    .row > * { flex: 1; }
    .msg { max-width: 760px; }
    code { color: #79c0ff; }
  </style>
</head>
<body>
  <header>
    <strong>NTH DAO Console</strong>
    <span id="summary" class="muted">loading...</span>
  </header>
  <main>
    <aside>
      <label>Agent ID</label>
      <div class="row"><input id="agent" value="admin"><button onclick="join()">Join</button></div>
      <h3>Members</h3><div id="members"></div>
      <h3>Channels</h3><div id="channels"></div>
    </aside>
    <section>
      <h3>Messages</h3>
      <div id="messages"></div>
      <textarea id="body" placeholder="Message #general"></textarea>
      <div class="row"><button onclick="sendMessage()">Send Message</button></div>
    </section>
    <aside>
      <h3>Announcement</h3>
      <input id="annTitle" placeholder="Title">
      <textarea id="annBody" placeholder="Body"></textarea>
      <button onclick="postAnnouncement()">Post</button>
      <h3>Tasks</h3>
      <input id="taskTitle" placeholder="Task title">
      <input id="taskAssignee" placeholder="Assignee">
      <button onclick="createTask()">Create Task</button>
      <div id="tasks"></div>
    </aside>
  </main>
  <script>
    let channel = "general";
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function api(path, opts) {
      const res = await fetch(path, opts);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }
    async function refresh() {
      const agent = $("agent").value || "admin";
      const s = await api(`/api/state?agent_id=${encodeURIComponent(agent)}&channel_id=${encodeURIComponent(channel)}`);
      $("summary").textContent = `${s.team.team_name} | members ${s.members.length} | role ${s.actor.role}`;
      $("members").innerHTML = s.members.map(m => `<div class="card"><code>${esc(m.agent_id)}</code><br><span class="muted">${esc(m.role)} ${m.online ? "online" : ""}</span></div>`).join("");
      $("channels").innerHTML = s.channels.map(c => `<button class="card" onclick="channel='${esc(c.channel_id)}';refresh()"># ${esc(c.name)}</button>`).join("");
      $("messages").innerHTML = s.messages.map(m => `<div class="card msg"><code>${esc(m.sender_id)}</code> <span class="muted">${esc(m.created_at)}</span><br>${esc(m.body)}</div>`).join("");
      $("tasks").innerHTML = s.tasks.map(t => `<div class="card"><strong>${esc(t.title)}</strong><br><span class="muted">${esc(t.status)} ${esc(t.assignee_id)}</span></div>`).join("");
    }
    async function join() {
      await api("/api/join", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({agent_id:$("agent").value})});
      refresh();
    }
    async function sendMessage() {
      await api("/api/messages", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({agent_id:$("agent").value, channel_id:channel, body:$("body").value})});
      $("body").value = ""; refresh();
    }
    async function postAnnouncement() {
      await api("/api/announcements", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({author_id:$("agent").value, channel_id:channel, title:$("annTitle").value, body:$("annBody").value})});
      $("annTitle").value = ""; $("annBody").value = ""; refresh();
    }
    async function createTask() {
      await api("/api/tasks", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({created_by:$("agent").value, channel_id:channel, title:$("taskTitle").value, assignee_id:$("taskAssignee").value})});
      $("taskTitle").value = ""; $("taskAssignee").value = ""; refresh();
    }
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>"""


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("NTH_HOST", "127.0.0.1")
    port = int(os.environ.get("NTH_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port)


__all__ = ["app", "create_app", "main"]
