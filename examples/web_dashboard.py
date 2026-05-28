"""
NTH DAO — Web Dashboard

单文件 FastAPI 服务，浏览器看 Kanban + Mission + Team Discovery。

启动：
    python web_dashboard.py
    # 默认 http://localhost:8000

设计：
- 只读视图（不改 state）—— 修改通过 Telegram bot 或 CLI
- 5 秒客户端 polling 刷新（不用 WebSocket，保持简单）
- 数据源：直接读 blackboard/*.jsonl + missions/*.json + team_agents/*.json
- 不绑定特定 agent_id —— 任何用户能看到全部状态
- 零额外依赖（除 fastapi + uvicorn）

API:
    GET  /                  HTML 主页
    GET  /api/team          在线 agent 列表
    GET  /api/blackboard    Blackboard entries (合并所有 scope)
    GET  /api/missions      Active mission 列表
    GET  /api/missions/{id} Mission 详情
    GET  /api/ledger        最近 ledger 条目
    GET  /api/evolution     evolution_audit 条目
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root
sys.path.insert(0, str(REPO))  # examples/ -> repo root accessible

# 复用 NTH DAO 子系统读数据
from team_layer.blackboard import Blackboard
from nth_dao.discovery import AgentRegistry
from nth_dao.orchestration import MissionStore

app = FastAPI(
    title="NTH DAO Dashboard",
    description="只读视图：查看团队状态、Mission 进度、Blackboard kanban",
    version="0.8.1",
)

# 全局 readers（不写）
BB = Blackboard(REPO / "blackboard")
REGISTRY = AgentRegistry(agents_dir=str(REPO / "team_agents"))
MISSIONS = MissionStore(str(REPO / "missions"))


# ─────────────────────────────────────────────────────────────────
# JSON APIs
# ─────────────────────────────────────────────────────────────────

@app.get("/api/team")
async def api_team():
    online = REGISTRY.list_alive()
    return {
        "count": len(online),
        "agents": [
            {
                "agent_id": r.agent_id,
                "backend_id": r.backend_id,
                "status": r.status,
                "capabilities": r.capabilities,
                "groups": r.groups,
                "hostname": r.hostname,
                "current_mission": r.current_mission,
                "last_seen": r.last_seen,
                "alive": r.is_alive(),
            }
            for r in online
        ],
    }


@app.get("/api/blackboard")
async def api_blackboard():
    entries = BB.list()
    # 按 status 分桶（Kanban）
    buckets = {"todo": [], "doing": [], "done": [], "blocked": [], "other": []}
    for e in entries:
        bucket = e.status if e.status in buckets else "other"
        buckets[bucket].append({
            "id": e.id,
            "scope": e.scope,
            "topic": e.topic,
            "author": e.author,
            "status": e.status,
            "content": (e.content or "")[:200],
            "updated_at": e.updated_at,
            "metadata": e.metadata,
        })
    return {"total": len(entries), "buckets": buckets}


@app.get("/api/missions")
async def api_missions():
    missions = MISSIONS.list_active()
    return {
        "count": len(missions),
        "missions": [
            {
                "id": m.id,
                "title": m.title,
                "status": m.status,
                "owner": m.owner,
                "scope": m.scope,
                "priority": m.priority,
                "progress": m.progress(),
                "step_count": len(m.steps),
                "created_at": m.created_at,
            }
            for m in missions
        ],
    }


@app.get("/api/missions/{mission_id}")
async def api_mission_detail(mission_id: str):
    # 支持前缀匹配
    for m in MISSIONS.list_all():
        if m.id.startswith(mission_id):
            return {
                "id": m.id,
                "title": m.title,
                "goal": m.goal,
                "status": m.status,
                "owner": m.owner,
                "scope": m.scope,
                "priority": m.priority,
                "progress": m.progress(),
                "created_at": m.created_at,
                "updated_at": m.updated_at,
                "steps": [
                    {
                        "id": s.id,
                        "description": s.description,
                        "status": s.status,
                        "assignee": s.assignee,
                        "previous_assignees": s.previous_assignees,
                        "depends_on": s.depends_on,
                        "notes": s.notes[-5:],  # 最近 5 条
                        "completed_at": s.completed_at,
                    }
                    for s in m.steps
                ],
            }
    raise HTTPException(404, f"Mission {mission_id!r} not found")


@app.get("/api/ledger")
async def api_ledger(limit: int = 20):
    ledger_path = REPO / "sidechain" / "ledger.jsonl"
    if not ledger_path.exists():
        return {"count": 0, "entries": []}
    entries = []
    try:
        lines = ledger_path.read_text(encoding="utf-8").strip().split("\n")
        for line in lines[-limit:]:
            if line.strip():
                entries.append(json.loads(line))
    except Exception as e:
        return {"count": 0, "entries": [], "error": str(e)}
    return {"count": len(entries), "entries": entries}


@app.get("/api/evolution")
async def api_evolution(limit: int = 20):
    audit_path = REPO / "sidechain" / "evolution_audit.jsonl"
    pending_dir = REPO / "sidechain" / "pending_patches"

    audit = []
    if audit_path.exists():
        try:
            for line in audit_path.read_text(encoding="utf-8").strip().split("\n")[-limit:]:
                if line.strip():
                    audit.append(json.loads(line))
        except Exception:
            pass

    pending = []
    if pending_dir.exists():
        for p in pending_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                patch = data.get("patch", {})
                pending.append({
                    "skill_id": patch.get("skill_id"),
                    "error_sig": patch.get("error_sig"),
                    "risk_level": patch.get("risk_level"),
                    "submitted_at": data.get("submitted_at"),
                })
            except Exception:
                continue

    return {"audit": audit, "pending": pending}


@app.get("/api/skills")
async def api_skills():
    skills_dir = REPO / "skills" / "registry"
    if not skills_dir.exists():
        return {"count": 0, "skills": []}
    skills = []
    for f in sorted(skills_dir.glob("*.md")):
        content = f.read_text(encoding="utf-8")
        # 简单解析 YAML 头
        info = {"name": f.stem, "raw_preview": content[:200]}
        for line in content.split("\n")[:10]:
            line = line.strip()
            if line.startswith("desc:"):
                info["desc"] = line[5:].strip().strip('"')
            elif line.startswith("risk:"):
                info["risk"] = line[5:].strip()
            elif line.startswith("error_sig:"):
                info["error_sig"] = line[10:].strip().strip('"')
        skills.append(info)
    return {"count": len(skills), "skills": skills}


@app.get("/api/summary")
async def api_summary():
    """主页顶部的总览数字"""
    return {
        "agents_online": len(REGISTRY.list_alive()),
        "missions_active": len(MISSIONS.list_active()),
        "blackboard_entries": len(BB.list()),
        "server_time": datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────
# HTML 主页（单文件，内嵌 vanilla JS + CSS）
# ─────────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NTH DAO Dashboard</title>
<style>
:root {
  --bg: #0f172a;
  --panel: #1e293b;
  --panel-2: #283449;
  --border: #334155;
  --text: #e2e8f0;
  --text-dim: #94a3b8;
  --accent: #38bdf8;
  --green: #4ade80;
  --yellow: #fbbf24;
  --red: #f87171;
  --gray: #64748b;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 16px;
}
h1 { margin: 0 0 12px; font-size: 22px; }
h2 { margin: 0 0 8px; font-size: 16px; color: var(--accent); }
.header {
  display: flex; align-items: center; gap: 24px; margin-bottom: 16px;
  padding: 12px 16px; background: var(--panel); border-radius: 8px;
  border: 1px solid var(--border);
}
.metric { display: flex; flex-direction: column; align-items: center; }
.metric .n { font-size: 28px; font-weight: bold; color: var(--accent); }
.metric .lbl { font-size: 12px; color: var(--text-dim); }
.last-update { margin-left: auto; font-size: 12px; color: var(--text-dim); }
.grid {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 12px;
}
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; max-height: 600px; overflow-y: auto;
}
.kanban {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
}
.kanban-col {
  background: var(--panel-2); border-radius: 6px; padding: 8px; min-height: 200px;
}
.kanban-col h3 {
  margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
  color: var(--text-dim); letter-spacing: 0.5px;
}
.kanban-col.todo h3    { color: var(--yellow); }
.kanban-col.doing h3   { color: var(--accent); }
.kanban-col.done h3    { color: var(--green); }
.kanban-col.blocked h3 { color: var(--red); }
.card {
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 6px 8px; margin-bottom: 6px; font-size: 12px;
}
.card .topic { font-weight: 500; color: var(--text); }
.card .meta  { font-size: 10px; color: var(--text-dim); margin-top: 2px; }
.agent-item, .mission-item {
  padding: 6px 8px; border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 13px;
}
.agent-item:last-child, .mission-item:last-child { border-bottom: none; }
.dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;
  vertical-align: middle;
}
.dot.alive { background: var(--green); }
.dot.dead  { background: var(--gray); }
.cap-tag {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  background: var(--panel-2); font-size: 10px; color: var(--text-dim);
  margin-right: 2px;
}
.progress-bar {
  flex: 1; height: 4px; background: var(--bg); border-radius: 2px;
  margin: 0 8px; overflow: hidden; min-width: 60px;
}
.progress-fill { height: 100%; background: var(--green); }
.empty { color: var(--text-dim); font-style: italic; padding: 16px; text-align: center; }
code { background: var(--bg); padding: 1px 4px; border-radius: 3px; font-size: 11px; }
@media (max-width: 800px) {
  .grid { grid-template-columns: 1fr; }
  .kanban { grid-template-columns: 1fr 1fr; }
}
</style>
</head>
<body>

<div class="header">
  <h1>🤖 NTH DAO Dashboard</h1>
  <div class="metric"><div class="n" id="m-agents">—</div><div class="lbl">agents online</div></div>
  <div class="metric"><div class="n" id="m-missions">—</div><div class="lbl">active missions</div></div>
  <div class="metric"><div class="n" id="m-board">—</div><div class="lbl">blackboard entries</div></div>
  <div class="last-update">⏱ <span id="last-update">connecting…</span></div>
</div>

<div class="grid">
  <div class="panel">
    <h2>👥 Online Agents</h2>
    <div id="agents-list" class="empty">loading…</div>
  </div>

  <div class="panel">
    <h2>📋 Blackboard Kanban</h2>
    <div class="kanban">
      <div class="kanban-col todo">    <h3>📋 TODO</h3>    <div id="bb-todo"></div></div>
      <div class="kanban-col doing">   <h3>🔨 DOING</h3>   <div id="bb-doing"></div></div>
      <div class="kanban-col done">    <h3>✅ DONE</h3>    <div id="bb-done"></div></div>
      <div class="kanban-col blocked"> <h3>🚧 BLOCKED</h3> <div id="bb-blocked"></div></div>
    </div>
  </div>

  <div class="panel">
    <h2>📦 Active Missions</h2>
    <div id="missions-list" class="empty">loading…</div>
  </div>

  <div class="panel">
    <h2>📚 Skills Registry</h2>
    <div id="skills-list" class="empty">loading…</div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString('zh-CN', {hour12: false}) : '—';
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch (e) {
    return { error: e.message };
  }
}

function renderAgents(data) {
  const el = $('agents-list');
  if (!data.agents || data.agents.length === 0) {
    el.innerHTML = '<div class="empty">no agents online</div>';
    return;
  }
  el.innerHTML = data.agents.map(a => `
    <div class="agent-item">
      <div>
        <span class="dot ${a.alive ? 'alive' : 'dead'}"></span>
        <code>${esc(a.agent_id)}</code>
        <span style="color:var(--text-dim); font-size:11px;"> on ${esc(a.hostname)}</span>
        <div style="margin-top:2px;">
          ${a.capabilities.map(c => `<span class="cap-tag">${esc(c)}</span>`).join('')}
        </div>
      </div>
      <span style="font-size:11px; color:var(--text-dim);">${esc(a.status)}</span>
    </div>
  `).join('');
}

function renderBlackboard(data) {
  ['todo', 'doing', 'done', 'blocked'].forEach(b => {
    const items = (data.buckets && data.buckets[b]) || [];
    const target = $('bb-' + b);
    if (items.length === 0) {
      target.innerHTML = '<div class="empty" style="padding:8px;">空</div>';
    } else {
      target.innerHTML = items.slice(0, 10).map(e => `
        <div class="card">
          <div class="topic">${esc(e.topic)}</div>
          <div class="meta">by ${esc(e.author)} · ${esc(e.scope)} · ${fmtTime(e.updated_at)}</div>
        </div>
      `).join('');
    }
  });
}

function renderMissions(data) {
  const el = $('missions-list');
  if (!data.missions || data.missions.length === 0) {
    el.innerHTML = '<div class="empty">no active missions</div>';
    return;
  }
  el.innerHTML = data.missions.map(m => `
    <div class="mission-item">
      <div style="flex:1;">
        <code>${esc(m.id.substring(0,8))}</code> ${esc(m.title)}
        <div style="font-size:11px; color:var(--text-dim); margin-top:2px;">
          ${esc(m.status)} · owned by <code>${esc(m.owner)}</code> · ${esc(m.priority)}
        </div>
      </div>
      <div style="display:flex; align-items:center; min-width:100px;">
        <div class="progress-bar"><div class="progress-fill" style="width:${m.progress.percent}%"></div></div>
        <span style="font-size:11px; color:var(--text-dim);">${m.progress.done}/${m.progress.total}</span>
      </div>
    </div>
  `).join('');
}

function renderSkills(data) {
  const el = $('skills-list');
  if (!data.skills || data.skills.length === 0) {
    el.innerHTML = '<div class="empty">no skills indexed</div>';
    return;
  }
  el.innerHTML = data.skills.map(s => `
    <div class="agent-item">
      <div>
        <code>${esc(s.name)}</code>
        ${s.risk ? `<span class="cap-tag" style="color:${s.risk==='high'?'var(--red)':s.risk==='medium'?'var(--yellow)':'var(--green)'};">${esc(s.risk)}</span>` : ''}
        <div style="font-size:11px; color:var(--text-dim);">${esc(s.desc || s.raw_preview.substring(0, 80))}</div>
      </div>
      ${s.error_sig ? `<code style="font-size:10px;">${esc(s.error_sig)}</code>` : ''}
    </div>
  `).join('');
}

async function refresh() {
  const [summary, team, bb, mis, sk] = await Promise.all([
    fetchJSON('/api/summary'),
    fetchJSON('/api/team'),
    fetchJSON('/api/blackboard'),
    fetchJSON('/api/missions'),
    fetchJSON('/api/skills'),
  ]);
  $('m-agents').textContent   = summary.agents_online ?? '?';
  $('m-missions').textContent = summary.missions_active ?? '?';
  $('m-board').textContent    = summary.blackboard_entries ?? '?';
  $('last-update').textContent = fmtTime(summary.server_time);
  renderAgents(team);
  renderBlackboard(bb);
  renderMissions(mis);
  renderSkills(sk);
}

refresh();
setInterval(refresh, 5000);  // 每 5 秒
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


# ─────────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────────

def main():
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    print(f"📡 NTH DAO Dashboard")
    print(f"   workspace: {REPO}")
    print(f"   URL:       http://{host}:{port}")
    print(f"   按 Ctrl+C 停止")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
