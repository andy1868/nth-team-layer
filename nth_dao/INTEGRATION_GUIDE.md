# NTH DAO — 集成指南

任何 Agent 框架都可以通过 **3 行代码** 加入 NTH DAO。

```python
import nth_dao as nth

team = nth.attach(agent_id="my-agent", backend="mock", capabilities=["python"])
# ... 你的 Agent 主循环 ...
team.detach()
```

加载后立即获得：
- ✅ 跨 Agent 共享的 **TEAM-SOUL.md**（团队灵魂规则）
- ✅ **Blackboard** 多 Agent 共享数据空间
- ✅ **Discovery** — 自动发现局域网/团队其他在线 Agent
- ✅ **Mission Orchestration** — 跨 session/终端/Agent 的超长期任务接力
- ✅ **EvoLoop** — 失败自动学习，跨 backend 共享修复
- ✅ **Git-backed 同步** — 团队仓库自动同步技能、记忆、任务

---

## 🚀 5 分钟快速开始

### 1. 安装（暂时使用本仓库 — PyPI 发布前）

```bash
git clone https://github.com/AlexNthLab/hermes-team-agent.git
cd hermes-team-agent
git checkout team-layer-v1
# 暂时不需要 pip install — 直接 import 即可（零第三方依赖）
```

### 2. 集成示例 — Hermes Agent

```python
# my_hermes_agent.py
import nth_dao as nth
from hermes.agent import Agent  # 你的 Hermes Agent

# Step 1: attach
team = nth.attach(
    agent_id="alice-hermes",
    backend="hermes",
    capabilities=["python", "web", "refactor"],
    groups=["frontend"],
)

# Step 2: 用 team.memory.build_memory_context_block() 拿到注入字符串
system_prompt = "You are a senior engineer." + "\n\n" + team.memory.build_memory_context_block()

# Step 3: 跑你原本的 Hermes 主循环
agent = Agent(system_prompt=system_prompt)
agent.run(goal="...")

# Step 4: 收尾
team.detach()
```

### 3. 集成示例 — Claude Code

```python
import nth_dao as nth
import subprocess

with nth.attach(
    agent_id="bob-cc",
    backend="claude_code",
    capabilities=["codegen", "refactor"],
) as team:
    system_prompt = team.memory.build_memory_context_block()

    # 你原本调 claude CLI 的方式
    result = subprocess.run(
        ["claude", "-p", "--append-system-prompt", system_prompt, "Fix the auth bug"],
        capture_output=True, text=True,
    )
    print(result.stdout)
```

### 4. 集成示例 — 任何自定义 LLM

```python
import nth_dao as nth
from openai import OpenAI

with nth.attach(agent_id="carol-openai", backend=None, capabilities=["chat"]) as team:
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": team.memory.build_memory_context_block()},
            {"role": "user", "content": "Tell me a joke"},
        ],
    )
    print(response.choices[0].message.content)
```

---

## 🤝 发现其他 Agent

```python
team.discover()              # 所有在线 Agent（含自己）
team.discover_others()       # 不含自己

# 按能力找
teammate = team.find_teammate(capability="codegen")
print(f"找到 {teammate.agent_id} 在 {teammate.hostname}")

# 多能力加权匹配（idle/group/同机优先）
best = team.find_teammate(needed_capabilities=["python", "web"])
```

---

## 🚢 超长期任务（Mission）

```python
# 发起一个长期任务（拆 3 个 step）
mission = team.start_mission(
    title="上线支付 v2",
    goal="整体重构支付流程",
    scope="shared",
    steps=[
        {
            "id": "design-api",
            "description": "设计 webhook API",
            "required_capabilities": ["backend"],
        },
        {
            "id": "impl-frontend",
            "description": "实现 React 结账组件",
            "required_capabilities": ["frontend"],
            "depends_on": ["design-api"],
        },
        {
            "id": "e2e-tests",
            "description": "写 E2E 测试",
            "required_capabilities": ["testing"],
            "depends_on": ["design-api", "impl-frontend"],
        },
    ],
    deadline="2026-06-30",
    priority="high",
)
```

**接力执行**：

```python
# 任何 Agent 启动时可以
mission = team.take_next_work()   # 自动 claim 一个 capability 匹配的 step

if mission:
    # 用你的 backend 执行
    # ...
    team.runner.complete(mission.id, step.id, output={"...": "..."})
    # 或交给另一个 Agent
    team.runner.handoff(mission.id, step.id, to_agent_id="alice-hermes", note="needs frontend")
```

跨终端、跨会话、跨 Agent 都能继续执行同一个 Mission。所有状态通过 `missions/*.json` 持久化，可被 git_sync 同步到团队仓库。

---

## 📦 工作目录布局

attach() 会在 workspace 下创建/读取：

```
workspace/
├── skills/
│   ├── TEAM-SOUL.md         ← 团队灵魂规则（Git 同步）
│   └── registry/            ← EvoLoop 自动产生的 skill（Git 同步）
├── memory/
│   └── user-model.json      ← 用户偏好（本地）
├── blackboard/
│   ├── shared.jsonl         ← 全团队任务（Git 同步）
│   ├── group_*.jsonl        ← 子团队（Git 同步）
│   └── private_*.jsonl      ← 单 Agent 私有（本地）
├── team_agents/             ← 心跳目录（Git 同步可选）
│   └── <agent_id>.json
├── missions/                ← 长期任务（Git 同步）
│   └── <mission_id>.json
├── team_logs/               ← 终端日志（Git 同步）
│   └── <hostname>_<user>_<ts>.jsonl
└── sidechain/
    ├── ledger.jsonl         ← Append-only 经验账本
    ├── evolution_audit.jsonl
    └── ...
```

---

## ⚙️ 高级配置

```python
team = nth.attach(
    agent_id="my-agent",
    backend="hermes",
    backend_kwargs={"model": "claude-sonnet-4-6"},
    capabilities=["python", "refactor"],
    groups=["backend", "ops"],
    workspace="./team-workspace",
    metadata={"version": "1.0", "preferred_lang": "python"},
    soul_path="skills/TEAM-SOUL.md",
    blackboard_root="blackboard",
    agents_dir="team_agents",
    missions_dir="missions",
    compression_threshold=0.80,     # 提高压缩触发阈值
    start_heartbeat=True,           # 后台心跳
)
```

---

## 🧪 故障排查

| 现象 | 原因 | 修复 |
|------|------|------|
| `team.discover()` 看不到其他 Agent | `team_agents/` 不共享 | 把整个 workspace 放进 Git 仓库（PR 5 git_sync 会自动 sync） |
| 心跳 30s 后离线 | 进程意外退出 | 用 `with nth.attach(...)` 语法或显式调 `team.detach()` |
| Mission 卡在 `todo` 不动 | 没人 capability 匹配 | 检查 `step.required_capabilities` 与各 Agent 的 capabilities |
| EvoLoop 不触发 | ROI 未到 | 设置 `EVOLUTION_BUDGET=5000` 降低门槛测试 |

---

## 🔗 进一步阅读

- `team_layer/` 子模块文档
- `multi_backend_demo.py` — 跨 backend 协作
- `nth_demo.py` — Discovery + Mission 端到端
- `IMPLEMENTATION_GUIDE.md` — Team Layer 完整设计
