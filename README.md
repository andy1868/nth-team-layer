# Nth Team Layer

> **Pluggable team-collaboration layer for any AI agent framework**
> Hermes • Claude Code • OpenClaw • Codex • OpenHands • your custom agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Zero deps](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

---

## 🎯 What is this?

**A drop-in plugin** that gives any AI agent:

- 🧠 **Layered memory** — soul rules, user model, vector skills, append-only ledger
- 📋 **Blackboard** — multi-agent shared workspace with kanban view
- 🛰️ **Discovery** — agents find each other across processes / terminals
- 🚢 **Mission orchestration** — relay long-running tasks across sessions and agents
- 🧬 **Self-evolution** — failures become reusable skills (ROI-gated)
- 🗜️ **Context compression** — 5-tier pipeline (cheap operators first)
- 🔄 **Git-backed sync** — distributed team workflow over standard git
- 🔌 **6 backend adapters** — Mock, Hermes, Claude Code, OpenClaw, Codex, OpenHands

**Zero third-party dependencies. All Python stdlib.**

---

## ⚡ 30-Second Quickstart

```python
import nth_team_layer as nth

with nth.attach(
    agent_id="alice",
    backend="mock",                       # or "hermes" / "claude_code" / ...
    capabilities=["python", "frontend"],
    groups=["payments"],
) as team:

    # 1. Find teammates
    teammate = team.find_teammate(capability="backend")
    print(f"Backend buddy: {teammate.record.agent_id if teammate else 'none online'}")

    # 2. Use team memory in your prompt
    system_prompt = "You are a senior engineer.\n" + team.memory.build_memory_context_block()

    # 3. Start a long-running mission
    mission = team.start_mission(
        title="ship payments v2",
        goal="end-to-end refactor",
        steps=[
            {"id": "api", "description": "design API", "required_capabilities": ["backend"]},
            {"id": "ui",  "description": "build UI",   "required_capabilities": ["frontend"],
             "depends_on": ["api"]},
        ],
    )

    # 4. Pull next available work for me
    if next_mission := team.take_next_work():
        # ... run your agent loop here ...
        team.runner.complete(next_mission.id, "ui", note="shipped")
```

---

## 📦 Installation

```bash
# From source (until PyPI release)
git clone https://github.com/AlexNthLab/nth-team-layer.git
cd nth-team-layer
pip install -e .

# Or just drop into your project (zero deps required)
cp -r nth_team_layer team_layer your-project/
```

Python 3.10+ required. **No third-party dependencies for core.** Optional extras:
`pip install "nth-team-layer[contracts]"` for Pydantic-backed EvoLoop validation.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                  nth_team_layer.attach()                         │
│                          ↓                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  TeamSession  — single facade for all subsystems         │  │
│  └──────────────────────────────────────────────────────────┘  │
│   │           │           │             │            │           │
│   ▼           ▼           ▼             ▼            ▼           │
│ Memory   Blackboard  Discovery   Orchestration   Compression    │
│ (4 prov) (kanban)    (heartbeat) (missions)     (5-tier)        │
│   │                                                              │
│   ▼                                                              │
│ EvoLoop  ←→  Backends (Mock/Hermes/CC/OpenClaw/Codex/OpenHands) │
│   │                                                              │
│   ▼                                                              │
│ Git-sync — share skills/missions/blackboard across the team      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🎁 What you get (8 subsystems)

### 1. Layered Memory (`MemoryProvider`)
4 default providers + your own:
- **SoulProvider** — TEAM-SOUL.md core anti-patterns (lazy load < 200 tokens)
- **UserModelProvider** — auto-sediment user preferences
- **VectorProvider** — skill registry index (53-token descriptions)
- **LedgerProvider** — append-only experience ledger

All injected into system prompt via `<memory-context>` fence to prevent identity confusion.

### 2. Blackboard
Multi-agent shared workspace with 3 scopes:
- `shared` — team-wide (Git synced)
- `group:<name>` — subteam (Git synced)
- `private:<agent_id>` — local only

Append-only version chains, kanban view, CLI tool (`python -m team_layer.blackboard`).

### 3. Discovery
Heartbeat-based agent registry (no central server):
```python
team.discover()                              # who's online?
team.find_teammate(capability="codegen")     # find a buddy
```

### 4. Mission Orchestration
Cross-session, cross-terminal, cross-agent task relay:
```python
team.start_mission(title="Q2 ship", steps=[...])
team.take_next_work()  # auto-match capability + dependency
team.runner.handoff(mission_id, step_id, to_agent_id="bob")
```

### 5. EvoLoop Self-Evolution
ROI-gated failure → fix skill:
- **Trigger**: `count >= 3 AND wasted > budget * 1.5`
- **Reflector** (subagent) generates SKILL.md + Pydantic contract
- **Verifier** sandboxes the patch
- **Gate** auto-merges (low risk) or queues for review (high risk)

### 6. 5-Tier Compression Pipeline
Cheap operators first; budget reduction → snip → microcompact → collapse → summary.

### 7. Git-backed Multi-Terminal Sync
- Zero-collision log naming: `{hostname}_{user}_{timestamp}.jsonl`
- Atomic skill hot-reload: `git checkout origin/main -- skills/`
- Daily evolution PR via GitHub Action

### 8. Backend Abstraction
Same Team Layer drives any framework:

| Backend | Module | Probe |
|---------|--------|-------|
| Mock | `team_layer.backends.mock` | Always available |
| Hermes | `team_layer.backends.hermes` | `import hermes` or `hermes` CLI |
| Claude Code | `team_layer.backends.claude_code` | `claude` CLI on PATH |
| OpenClaw | `team_layer.backends.openclaw` | `OPENCLAW_API_URL` env |
| Codex | `team_layer.backends.codex` | `codex` CLI on PATH |
| OpenHands | `team_layer.backends.openhands` | `OPENHANDS_API_URL` reachable |

```bash
python -c "import nth_team_layer as nth; print(nth.default_registry.list_available(refresh=True))"
```

---

## 🚀 Real-world usage

### Cross-framework team
```python
# Alice on Hermes
team = nth.attach(agent_id="alice", backend="hermes", capabilities=["py"])

# Bob on Claude Code
team = nth.attach(agent_id="bob", backend="claude_code", capabilities=["ts"])

# Carol on OpenHands
team = nth.attach(agent_id="carol", backend="openhands", capabilities=["test"])

# All three see the same blackboard, same missions, same evolved skills.
```

### Multi-day mission
```python
# Day 1, Alice's machine
mission = team.start_mission(title="3-month refactor", steps=[...])

# Day 5, Bob's machine (after Alice's session ended)
mission = team.take_next_work()  # Bob picks up where Alice left off
```

### Self-improving team
Every failure goes to the ledger. When the same `error_sig` recurs and wastes
enough tokens, EvoLoop generates a fix skill that the entire team auto-loads
on next session start. **Failures compound into knowledge.**

---

## 📚 Examples

| File | What it shows |
|------|---------------|
| [`examples/evo_demo.py`](examples/evo_demo.py) | EvoLoop pipeline: trigger → reflector → verifier → gate |
| [`examples/sync_demo.py`](examples/sync_demo.py) | Multi-terminal git sync + central aggregator |
| [`examples/blackboard_demo.py`](examples/blackboard_demo.py) | 3 agents collaborating via blackboard |
| [`examples/multi_backend_demo.py`](examples/multi_backend_demo.py) | Cross-backend learning (shared ledger) |
| [`examples/nth_demo.py`](examples/nth_demo.py) | **Discovery + Mission relay (start here)** |
| [`examples/integration_demo.py`](examples/integration_demo.py) | All PRs wired into one entry point |
| [`examples/team_entrypoint.py`](examples/team_entrypoint.py) | Production-ready CLI |

Run any of them:
```bash
python examples/nth_demo.py
```

---

## 🔌 Plug-and-play integration

Already have an OpenAI agent? Drop in Team Layer in 3 lines:

```python
import nth_team_layer as nth
from openai import OpenAI

with nth.attach(agent_id="my-agent", backend=None, capabilities=["chat"]) as team:
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": team.memory.build_memory_context_block()},
            {"role": "user", "content": "What's left on my plate?"},
        ],
    )
```

See [`nth_team_layer/INTEGRATION_GUIDE.md`](nth_team_layer/INTEGRATION_GUIDE.md) for Hermes, Claude Code, and custom-LLM examples.

---

## 🧬 Why "Nth"?

This grew out of the [Nth Agent Pro](https://github.com/AlexNthLab) project — a self-healing,
team-evolvable agent runtime. After implementing the same patterns across multiple agent
frameworks, we extracted the common ground into this standalone plugin.

The vision: **n-th order improvement** — each session's failure becomes the next
session's skill; each agent's experience becomes the team's collective wisdom.

---

## 📦 Releasing to PyPI

Automated via GitHub Action `.github/workflows/publish.yml`. **One-time setup**:

1. Register on https://pypi.org (and https://test.pypi.org for staging)
2. In your PyPI account, go to **Publishing** → **Add a new pending publisher**:
   - PyPI Project Name: `nth-team-layer`
   - Owner: `AlexNthLab`
   - Repository name: `nth-team-layer`
   - Workflow name: `publish.yml`
   - Environment name: `pypi` (for prod) / `testpypi` (for staging)
3. In GitHub repo Settings → **Environments**, create two environments named `pypi` and `testpypi`. No secrets needed — OIDC handles auth.

**Release a new version**:

```bash
# 1. Bump version in pyproject.toml
# 2. Update CHANGELOG.md
# 3. Tag and push:
git tag v0.8.0
git push origin v0.8.0
```

GitHub Action picks up the tag, builds wheel + sdist, verifies version matches tag, and publishes to PyPI via trusted publishing. Done.

For staging (TestPyPI) try: **Actions → 📦 Publish to PyPI → Run workflow** → choose `testpypi`.

## 📜 License

MIT — see [LICENSE](LICENSE).

## 🤝 Contributing

PRs welcome. Especially:
- New backend adapters (Gemini, Bedrock, vLLM, ...)
- Real LLM-driven Reflector (replacing the template fallback)
- Web dashboard for live kanban + mission tracking
- Tests (the demos work but should become a proper pytest suite)

See [CHANGELOG.md](CHANGELOG.md) for version history.

---

## 🌟 Star History

If this helped you build a self-improving agent team, please ⭐ the repo.
