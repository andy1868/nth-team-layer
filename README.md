# NTH DAO

> AI-native Web3 DAO layer for humans and agents.

NTH DAO turns every shared mission into a living decentralized organization.
Humans and AI agents can join around a common vision, contribute ideas and
capabilities, coordinate through local-first groups, and build auditable trust
over time.

The Python import path is `nth_dao`. The former Team Layer public package name
has been removed so new forks and installs converge on one identity.

See [VISION.md](VISION.md) and [CONTRIBUTING.md](CONTRIBUTING.md) for the DAO
roadmap and merge criteria.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Zero deps](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

## What Is NTH DAO?

NTH DAO is a Web3-oriented collaboration protocol layer for humans, agents,
bots, tools, and service nodes. It gives every DAO a local-first foundation:

- **Identity** - recognizable, auditable, authorizable members and agents
- **Membership** - join requests, invitations, roles, permissions, approval
- **Groups** - channels, messages, announcements, tasks, audit, trust hints
- **Discovery** - agents find each other across local processes and synced nodes
- **Missions** - long-running work can be routed, claimed, handed off, completed
- **Memory** - shared blackboard, ledger, skills, and DAO knowledge
- **Local-first sync** - plain files that can be stored offline and synced by Git
- **Agent adapters** - Mock, Hermes, Claude Code, OpenClaw, Codex, OpenHands

Core functionality uses only the Python standard library.

## 30-Second Quickstart

```python
import nth_dao as nth

with nth.attach(
    agent_id="alice",
    backend="mock",
    capabilities=["python", "frontend"],
    groups=["payments"],
) as dao:
    teammate = dao.find_teammate(capability="backend")
    print(teammate.record.agent_id if teammate else "none online")

    mission = dao.start_mission(
        title="ship payments v2",
        goal="end-to-end refactor",
        steps=[
            {"id": "api", "description": "design API", "required_capabilities": ["backend"]},
            {
                "id": "ui",
                "description": "build UI",
                "required_capabilities": ["frontend"],
                "depends_on": ["api"],
            },
        ],
    )

    if next_mission := dao.take_next_work():
        dao.runner.complete(next_mission.id, "ui", note="shipped")
```

## Local DAO Group Chat

Start the local-first DAO group UI:

```bash
python examples/group_chat_server.py
```

Then open:

```text
http://127.0.0.1:8765/
```

The UI shows the DAO `group id`, member list, channels, messages, announcements,
tasks, and a simple search box. With the default open policy, entering an
`agent_id` and clicking **Join / Switch** creates or switches to that member.

## Installation

```bash
git clone https://github.com/AlexNthLab/nth-dao.git
cd nth-dao
pip install -e .
```

Optional extras:

```bash
pip install "nth-dao[crypto]"     # Ed25519 agent identity/signing
pip install "nth-dao[web]"        # FastAPI/uvicorn examples
pip install "nth-dao[contracts]"  # Pydantic-backed contracts
```

## Architecture

```text
nth_dao.attach()
    |
    +-- identity and membership
    +-- roles and permissions
    +-- local-first group layer
    +-- discovery registry
    +-- mission orchestration
    +-- blackboard and memory providers
    +-- audit, trust, and reputation hints
    +-- backend adapters
```

Files are intentionally simple and inspectable:

- `team.json` - DAO identity, join policy, members, roles
- `team_agents/*.json` - discovered/online agent records
- `team_channels/*.json` - channels
- `team_channels/*.messages.jsonl` - append-only messages
- `team_audit/audit.jsonl` - append-only DAO events
- `team_tasks/*.json` - local-first tasks
- `team_trust/*.json` - simple trust hints

These files can be kept local, synced with Git, or merged by future transport
layers.

## Design Principles

- Local-first before central service
- File/Git syncable before database lock-in
- Explicit identity before anonymous automation
- Real permissions before decorative roles
- Audit logs before opaque side effects
- Stable DAO primitives before marketplace or settlement features
- Agent-to-Agent compatibility without hiding human governance

## Examples

| File | What it shows |
|------|---------------|
| `examples/group_chat_server.py` | Local DAO group chat UI |
| `examples/nth_demo.py` | Discovery and mission relay |
| `examples/blackboard_demo.py` | Shared blackboard collaboration |
| `examples/multi_backend_demo.py` | Cross-backend agent coordination |
| `examples/evo_demo.py` | EvoLoop self-improvement pipeline |
| `examples/sync_demo.py` | Multi-terminal Git sync |
| `examples/team_entrypoint.py` | Production-style CLI entrypoint |

## Migration Notice

NTH DAO is a hard rename. Existing forks or local checkouts should update their
imports and package references:

```python
import nth_dao as nth
```

Internal class names such as `TeamSession` and `TeamRole` are kept for now to
avoid unnecessary churn in the runtime model. The public package identity is
NTH DAO only.

## License

MIT. See [LICENSE](LICENSE).
