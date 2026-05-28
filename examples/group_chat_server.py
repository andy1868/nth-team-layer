"""Local group chat server for Nth Team Layer.

Run:
    python examples/group_chat_server.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import nth_team_layer as nth


WORKSPACE = Path(os.environ.get("NTH_WORKSPACE", REPO)).resolve()
MEMBERSHIP = nth.MembershipManager(WORKSPACE)
GROUPS = nth.GroupManager(WORKSPACE, membership=MEMBERSHIP)

app = FastAPI(title="Nth Team Group Chat", version=nth.__version__)


class MessageIn(BaseModel):
    agent_id: str
    body: str
    channel_id: str = "general"


class AnnouncementIn(BaseModel):
    author_id: str
    title: str
    body: str
    channel_id: str = "general"


class TaskIn(BaseModel):
    created_by: str
    title: str
    description: str = ""
    assignee_id: str = ""
    channel_id: str = "general"


def bootstrap() -> None:
    config = MEMBERSHIP.load_config()
    if not config.admin_ids and not config.member_ids:
        MEMBERSHIP.init_team(team_name="Nth Team", policy="open", admin_ids=["admin"])
    elif not config.admin_ids:
        if "admin" not in config.member_ids:
            config.member_ids.append("admin")
        config.admin_ids.append("admin")
        config.roles["admin"] = nth.TeamRole.OWNER.value
        MEMBERSHIP.save_config(config)
    if GROUPS.get_channel("general") is None:
        GROUPS.create_channel("general", created_by="admin", topic="Team chat")


def ensure_open_member(agent_id: str) -> None:
    if not agent_id:
        raise HTTPException(400, "agent_id is required")
    ok, reason = MEMBERSHIP.ensure_member(agent_id)
    if not ok:
        raise HTTPException(403, reason)


@app.on_event("startup")
async def startup() -> None:
    bootstrap()


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/api/state")
async def api_state(agent_id: str = "admin", channel_id: str = "general"):
    ensure_open_member(agent_id)
    config = MEMBERSHIP.load_config()
    return {
        "team": config.to_dict(),
        "role": config.role_for(agent_id).value,
        "channels": [c.to_dict() for c in GROUPS.list_channels(actor_id=agent_id)],
        "messages": [m.to_dict() for m in GROUPS.list_messages(channel_id, actor_id=agent_id, limit=100)],
        "announcements": [a.to_dict() for a in GROUPS.list_announcements(channel_id)],
        "tasks": [t.to_dict() for t in GROUPS.list_tasks()],
        "audit": [e.to_dict() for e in GROUPS.list_audit_events(limit=20)],
    }


@app.post("/api/messages")
async def api_post_message(payload: MessageIn):
    ensure_open_member(payload.agent_id)
    msg = GROUPS.post_message(
        payload.channel_id,
        sender_id=payload.agent_id,
        body=payload.body,
    )
    return msg.to_dict()


@app.post("/api/announcements")
async def api_post_announcement(payload: AnnouncementIn):
    announcement = GROUPS.post_announcement(
        payload.title,
        payload.body,
        author_id=payload.author_id,
        channel_id=payload.channel_id,
    )
    return announcement.to_dict()


@app.post("/api/tasks")
async def api_create_task(payload: TaskIn):
    ensure_open_member(payload.created_by)
    if payload.assignee_id:
        ensure_open_member(payload.assignee_id)
    task = GROUPS.create_task(
        payload.title,
        created_by=payload.created_by,
        description=payload.description,
        assignee_id=payload.assignee_id,
        channel_id=payload.channel_id,
    )
    return task.to_dict()


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nth Team Chat</title>
<style>
:root {
  --bg: #f7f8fb;
  --line: #d9dee7;
  --ink: #20242c;
  --muted: #667085;
  --panel: #ffffff;
  --accent: #2563eb;
  --accent-soft: #e8f0ff;
  --good: #0f766e;
  --warn: #b45309;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  color: var(--ink);
  background: var(--bg);
}
header {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
h1 { font-size: 17px; margin: 0; font-weight: 650; }
main {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 300px;
  min-height: calc(100vh - 56px);
}
aside, section {
  border-right: 1px solid var(--line);
  background: var(--panel);
}
.left, .right { padding: 14px; }
.chat {
  display: grid;
  grid-template-rows: auto 1fr auto;
  min-width: 0;
  background: #fbfcff;
}
.bar {
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--panel);
}
.messages {
  padding: 16px;
  overflow-y: auto;
  min-height: 360px;
}
.message {
  max-width: 760px;
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.meta { color: var(--muted); font-size: 12px; margin-bottom: 5px; }
.composer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  background: var(--panel);
}
input, textarea, select, button {
  font: inherit;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
input, textarea, select { padding: 8px 10px; width: 100%; }
textarea { min-height: 72px; resize: vertical; }
button {
  padding: 8px 12px;
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  cursor: pointer;
}
button.secondary {
  background: #fff;
  color: var(--accent);
}
.stack { display: grid; gap: 10px; }
.item {
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}
.channel.active { background: var(--accent-soft); border-color: #b7cdfd; }
.small { font-size: 12px; color: var(--muted); }
.role { color: var(--good); font-size: 12px; }
.task-open { color: var(--warn); }
@media (max-width: 900px) {
  main { grid-template-columns: 1fr; }
  aside { border-right: 0; border-bottom: 1px solid var(--line); }
  .right { display: none; }
}
</style>
</head>
<body>
<header>
  <h1>Nth Team 群聊</h1>
  <input id="agent" value="admin" style="max-width:220px" />
  <span id="role" class="role"></span>
  <button class="secondary" onclick="refresh()">刷新</button>
</header>
<main>
  <aside class="left">
    <div class="stack">
      <strong>频道</strong>
      <div id="channels"></div>
      <div class="small">当前工作区: local Git workspace</div>
    </div>
  </aside>
  <section class="chat">
    <div class="bar">
      <strong id="title"># general</strong>
      <span id="status" class="small"></span>
    </div>
    <div id="messages" class="messages"></div>
    <div class="composer">
      <textarea id="body" placeholder="输入群消息"></textarea>
      <button onclick="sendMessage()">发送</button>
    </div>
  </section>
  <aside class="right">
    <div class="stack">
      <strong>公告</strong>
      <input id="ann-title" placeholder="标题" />
      <textarea id="ann-body" placeholder="公告内容"></textarea>
      <button onclick="postAnnouncement()">发布公告</button>
      <div id="announcements"></div>
      <strong>任务</strong>
      <input id="task-title" placeholder="任务标题" />
      <input id="task-assignee" placeholder="负责人 agent_id" />
      <textarea id="task-desc" placeholder="任务描述"></textarea>
      <button onclick="createTask()">创建任务</button>
      <div id="tasks"></div>
    </div>
  </aside>
</main>
<script>
let channel = "general";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const when = (s) => s ? new Date(s).toLocaleString() : "";

async function json(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function refresh() {
  try {
    const agent = $("agent").value.trim() || "admin";
    const state = await json(`/api/state?agent_id=${encodeURIComponent(agent)}&channel_id=${encodeURIComponent(channel)}`);
    $("role").textContent = `role: ${state.role}`;
    $("status").textContent = `${state.messages.length} messages`;
    $("title").textContent = `# ${channel}`;
    $("channels").innerHTML = state.channels.map(c => `
      <div class="item channel ${c.channel_id === channel ? "active" : ""}" onclick="channel='${esc(c.channel_id)}'; refresh()">
        <strong>${esc(c.name)}</strong><div class="small">${esc(c.topic || c.channel_id)}</div>
      </div>
    `).join("");
    $("messages").innerHTML = state.messages.map(m => `
      <div class="message">
        <div class="meta">${esc(m.sender_id)} · ${when(m.created_at)}</div>
        <div>${esc(m.body).replace(/\\n/g, "<br>")}</div>
      </div>
    `).join("") || '<div class="small">暂无消息</div>';
    $("messages").scrollTop = $("messages").scrollHeight;
    $("announcements").innerHTML = state.announcements.map(a => `
      <div class="item"><strong>${esc(a.title)}</strong><div>${esc(a.body)}</div><div class="small">${esc(a.author_id)} · ${when(a.created_at)}</div></div>
    `).join("") || '<div class="small">暂无公告</div>';
    $("tasks").innerHTML = state.tasks.map(t => `
      <div class="item"><strong>${esc(t.title)}</strong><div class="small">${esc(t.assignee_id || "unassigned")} · <span class="task-open">${esc(t.status)}</span></div></div>
    `).join("") || '<div class="small">暂无任务</div>';
  } catch (e) {
    $("status").textContent = e.message;
  }
}

async function sendMessage() {
  const body = $("body").value.trim();
  if (!body) return;
  await json("/api/messages", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({agent_id: $("agent").value.trim() || "admin", channel_id: channel, body})
  });
  $("body").value = "";
  refresh();
}

async function postAnnouncement() {
  await json("/api/announcements", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      author_id: $("agent").value.trim() || "admin",
      channel_id: channel,
      title: $("ann-title").value.trim(),
      body: $("ann-body").value.trim()
    })
  });
  $("ann-title").value = "";
  $("ann-body").value = "";
  refresh();
}

async function createTask() {
  await json("/api/tasks", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      created_by: $("agent").value.trim() || "admin",
      channel_id: channel,
      title: $("task-title").value.trim(),
      assignee_id: $("task-assignee").value.trim(),
      description: $("task-desc").value.trim()
    })
  });
  $("task-title").value = "";
  $("task-assignee").value = "";
  $("task-desc").value = "";
  refresh();
}

$("body").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendMessage();
});
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def main() -> None:
    host = os.environ.get("NTH_CHAT_HOST", "127.0.0.1")
    port = int(os.environ.get("NTH_CHAT_PORT", "8765"))
    print(f"Nth Team group chat: http://{host}:{port}")
    print(f"workspace: {WORKSPACE}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
