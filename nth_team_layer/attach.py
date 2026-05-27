"""
attach() — 一键集成 API

任何 Agent 框架的入口都可以通过一行代码加入 Nth Team Layer：

    import nth_team_layer as nth
    team = nth.attach(
        agent_id="my-agent",            # 或省略 → 自动生成 Ed25519 身份
        backend="mock",                 # 或传入已有 AgentBackend 实例
        capabilities=["python", "web"],
        groups=["frontend"],
        workspace="./my-team-workspace",
    )

    # 立即可用：
    team.memory                # TeamMemoryManager — 注入 system prompt
    team.blackboard            # Blackboard
    team.runner                # MissionRunner
    team.finder                # PeerFinder
    team.channel               # TeamChannel — Agent 间群聊/私聊
    team.reputation            # ReputationManager — 声誉评分
    team.marketplace           # TaskMarketplace — 任务交易
    team.discover()            # list_alive agents
    team.start_mission(...)
    team.detach()              # 注销心跳，结束会话

设计：
- attach() 完成所有子系统初始化（13 个子系统）
- TeamSession 是一个简洁的 facade，把各子系统组合成统一访问点
- detach() 干净清理（心跳停止、ledger 刷盘等）
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from team_layer import TeamAgent, TeamMemoryManager
from team_layer.backends import AgentBackend, default_registry
from team_layer.blackboard import Blackboard, BlackboardProvider
from team_layer.memory_providers import (
    LedgerProvider,
    SoulProvider,
    UserModelProvider,
    VectorProvider,
)

from .discovery import AgentRegistry, PeerFinder
from .orchestration import Mission, MissionRunner, MissionStore
from .identity import AgentIdentity, AgentID, load_or_generate
from .channel import TeamChannel
from .reputation import ReputationManager
from .marketplace import TaskMarketplace


@dataclass
class TeamSession:
    """attach() 返回的 facade 对象 — 统一访问所有 Team Layer 能力"""
    agent_id: str
    backend_id: str
    workspace: Path

    agent: TeamAgent
    memory: TeamMemoryManager
    blackboard: Blackboard
    registry: AgentRegistry
    finder: PeerFinder
    mission_store: MissionStore
    runner: MissionRunner
    identity: Optional[AgentIdentity] = None
    channel: Optional[TeamChannel] = None
    reputation: Optional[ReputationManager] = None
    marketplace: Optional[TaskMarketplace] = None
    backend: Optional[AgentBackend] = None
    capabilities: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    _detached: bool = False

    # ─── 便利方法 ───

    def discover(self) -> List:
        """列出当前在线的所有 Agent（包括自己）"""
        return self.registry.list_alive()

    def discover_others(self) -> List:
        """列出除自己以外的在线 Agent"""
        return [r for r in self.registry.list_alive() if r.agent_id != self.agent_id]

    def find_teammate(
        self,
        capability: Optional[str] = None,
        needed_capabilities: Optional[List[str]] = None,
        group: Optional[str] = None,
    ):
        """快速查找队友"""
        if needed_capabilities:
            return self.finder.best_match(
                needed_capabilities=needed_capabilities,
                prefer_idle=True,
                exclude_agent_ids=[self.agent_id],
            )
        if capability:
            results = self.finder.find(
                capability=capability,
                exclude_agent_ids=[self.agent_id],
            )
            return results[0] if results else None
        if group:
            results = self.finder.find(
                group=group,
                exclude_agent_ids=[self.agent_id],
            )
            return results[0] if results else None
        return None

    def start_mission(
        self,
        title: str,
        goal: str,
        steps: List[dict],
        scope: str = "shared",
        deadline: Optional[str] = None,
        priority: str = "normal",
        tags: Optional[List[str]] = None,
    ) -> Mission:
        """启动一个超长期任务"""
        m = Mission.new(
            title=title,
            goal=goal,
            owner=self.agent_id,
            scope=scope,
            steps=steps,
            deadline=deadline,
            priority=priority,
            tags=tags or [],
        )
        self.mission_store.create(m)

        # 把 mission 也写入 blackboard，让 Kanban 能看到
        self.blackboard.post(
            topic=f"[MISSION] {title}",
            author=self.agent_id,
            scope=scope,
            status="doing",
            content=goal,
            metadata={"mission_id": m.id, "type": "mission"},
        )

        # 心跳更新
        self.registry.update_status(current_mission=m.id)

        return m

    def take_next_work(self) -> Optional[Mission]:
        """主动找一个可执行的 step 并 claim — 用于"自动接力"循环"""
        found = self.runner.find_work()
        if not found:
            return None
        mission, step = found
        self.runner.claim(mission.id, step.id)
        self.registry.update_status(status="busy", current_mission=mission.id)
        return mission

    def detach(self) -> None:
        """注销 + 干净收尾"""
        if self._detached:
            return
        # 持久化 agent 记忆
        try:
            self.agent.finalize()
        except Exception as e:
            print(f"[ATTACH] finalize warning: {e}")
        # 注销心跳
        try:
            self.registry.unregister()
        except Exception as e:
            print(f"[ATTACH] unregister warning: {e}")
        self._detached = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.detach()


# ───────────────────────────────────────────────────────────────
# attach() — 主入口
# ───────────────────────────────────────────────────────────────

def attach(
    agent_id: Optional[str] = None,
    identity: Optional[AgentIdentity] = None,
    backend: Union[str, AgentBackend, None] = None,
    backend_kwargs: Optional[Dict[str, Any]] = None,
    capabilities: Optional[List[str]] = None,
    groups: Optional[List[str]] = None,
    workspace: Union[str, Path] = ".",
    metadata: Optional[Dict[str, Any]] = None,
    soul_path: str = "skills/TEAM-SOUL.md",
    user_model_path: str = "memory/user-model.json",
    vector_dir: str = "skills/registry",
    ledger_path: str = "sidechain/ledger.jsonl",
    blackboard_root: str = "blackboard",
    agents_dir: str = "team_agents",
    missions_dir: str = "missions",
    compression_threshold: float = 0.75,
    start_heartbeat: bool = True,
) -> TeamSession:
    """
    把当前进程加入 Nth Team Layer，自动获得密码学身份、
    消息通道、声誉系统、交易市场等全部去中心化 Agent 能力。

    Args:
        agent_id: 唯一标识本 Agent。不传则自动生成 Ed25519 密钥对
        identity: 显式传入的 AgentIdentity（优先级高于 agent_id）
        backend: 字符串（从 registry 创建）或 AgentBackend 实例，或 None
        backend_kwargs: 当 backend 是字符串时传给 ctor 的参数
        capabilities: 能力标签（如 ["python", "web", "codegen"]）
        groups: 子团队（如 ["frontend", "ops"]）
        workspace: 工作目录（所有 dir 路径相对于此）
        其余路径参数：覆盖默认 Team Layer 子系统目录

    Returns:
        TeamSession — 统一 facade，可用 with 语法自动 detach
    """
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    capabilities = capabilities or []
    groups = groups or []

    # 0. 身份解析（优先级：显式 identity > agent_id 字符串 > 自动生成 Ed25519）
    if identity is not None:
        agent_identity = identity
        resolved_agent_id = str(agent_identity.agent_id)
    elif agent_id is not None:
        agent_identity = AgentIdentity.from_string(agent_id)
        resolved_agent_id = agent_id
    else:
        agent_identity = load_or_generate(workspace)
        resolved_agent_id = str(agent_identity.agent_id)

    # 1. backend 实例化
    backend_instance: Optional[AgentBackend] = None
    backend_id_str = "none"
    if isinstance(backend, AgentBackend):
        backend_instance = backend
        backend_id_str = backend.backend_id
    elif isinstance(backend, str):
        backend_instance = default_registry.create(backend, **(backend_kwargs or {}))
        backend_id_str = backend

    # 2. 4+1 个 Provider
    providers = [
        SoulProvider(str(workspace / soul_path)),
        UserModelProvider(str(workspace / user_model_path)),
        VectorProvider(str(workspace / vector_dir)),
        LedgerProvider(str(workspace / ledger_path)),
        BlackboardProvider(
            agent_id=resolved_agent_id,
            blackboard_root=str(workspace / blackboard_root),
            groups=groups,
        ),
    ]

    # 3. TeamMemoryManager + TeamAgent
    mem = TeamMemoryManager(providers, session_id=f"{resolved_agent_id}_session")
    mem.initialize({"agent_id": resolved_agent_id, "capabilities": capabilities})
    team_agent = TeamAgent(
        agent_id=resolved_agent_id,
        team_memory_manager=mem,
        compression_threshold=compression_threshold,
    )

    # 4. Blackboard
    blackboard = Blackboard(workspace / blackboard_root)

    # 5. Discovery — 注册自己 + 启动心跳
    registry = AgentRegistry(agents_dir=str(workspace / agents_dir))
    registry.register(
        agent_id=resolved_agent_id,
        backend_id=backend_id_str,
        capabilities=capabilities,
        groups=groups,
        metadata=metadata or {},
        start_heartbeat=start_heartbeat,
    )

    # 6. PeerFinder
    finder = PeerFinder(registry)

    # 7. Mission Orchestration
    mission_store = MissionStore(str(workspace / missions_dir))
    runner = MissionRunner(
        store=mission_store,
        agent_id=resolved_agent_id,
        capabilities=capabilities,
    )

    # 8. Channel — Agent 间消息通道（群聊/私聊/DM）
    channel = TeamChannel(
        workspace=workspace,
        agent_id=resolved_agent_id,
        identity=agent_identity if agent_identity.can_sign else None,
    )

    # 9. Reputation — 声誉系统（评分/信任网络）
    reputation = ReputationManager(
        workspace=workspace,
        agent_id=resolved_agent_id,
        identity=agent_identity if agent_identity.can_sign else None,
    )

    # 10. Marketplace — Agent 交易市场（任务发布/认领/结算）
    marketplace = TaskMarketplace(
        workspace=workspace,
        agent_id=resolved_agent_id,
        identity=agent_identity if agent_identity.can_sign else None,
        channel=channel,
        reputation=reputation,
    )

    return TeamSession(
        agent_id=resolved_agent_id,
        backend_id=backend_id_str,
        workspace=workspace,
        agent=team_agent,
        memory=mem,
        blackboard=blackboard,
        registry=registry,
        finder=finder,
        mission_store=mission_store,
        runner=runner,
        identity=agent_identity,
        channel=channel,
        reputation=reputation,
        marketplace=marketplace,
        backend=backend_instance,
        capabilities=capabilities,
        groups=groups,
    )
