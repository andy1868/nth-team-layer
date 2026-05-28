"""
NTH DAO — 可插拔的 Agent 团队协作层

任何 Agent 框架（Hermes / Claude Code / OpenClaw / Codex / OpenHands / 自定义）
都可以通过一行 attach() 调用接入：

    >>> import nth_dao as nth
    >>> team = nth.attach(agent_id="my-agent", backend="mock")
    >>> team.discover()                      # 发现其他在线 Agent
    >>> team.blackboard.post("task", "...")  # 协作
    >>> team.start_mission("ship feature X") # 启动超长期任务

核心能力（继承自 team_layer 已有 PR 1-7）：
    - 4 层记忆 Provider（SOUL / User / Vector / Ledger）
    - Blackboard 多 Agent 共享数据空间
    - 5 层上下文压缩管线
    - EvoLoop 跨 backend 自进化
    - Git-backed 多终端协同
    - 6 backend 统一适配

新增 PR 8 能力：
    - Agent Discovery：基于心跳文件 + Git 同步发现队友
    - Mission Orchestration：跨 session/终端/Agent 的超长期任务接力
    - attach() 一键集成：让任何 Agent 框架 3 行代码加入团队

包名（pyproject.toml）: nth-dao
导入名: nth_dao
"""

__version__ = "0.8.1"
__author__ = "NTH DAO Project"

# ───────────────────────────────────────────────────────────────
# Facade：重新导出 team_layer 的全部公共 API
# 保证向后兼容：所有 PR 1-7 的代码可以无缝用 nth_dao
# ───────────────────────────────────────────────────────────────

# PR 1-2: 核心运行时 + 4 个记忆 Provider
from team_layer import TeamAgent, TeamMemoryManager
from team_layer.memory_providers import (
    SoulProvider,
    UserModelProvider,
    VectorProvider,
    LedgerProvider,
)

# PR 3: 压缩管线
from team_layer.compression import CompressionPipeline, CompressionStage

# PR 4: EvoLoop 自进化
from team_layer.evolution import (
    EvoLoop,
    EvoTrigger,
    Reflector,
    Verifier,
    EvolutionGate,
)

# PR 5: 多终端协同
from team_layer.git_sync import (
    SyncConfig,
    LogCollector,
    SkillLoader,
    CentralAggregator,
)

# PR 6: Blackboard
from team_layer.blackboard import (
    Blackboard,
    BlackboardEntry,
    BlackboardProvider,
    Scope,
    render_kanban,
    render_table,
)

# PR 7: AgentBackend ABC
from team_layer.backends import (
    AgentBackend,
    BackendCapabilities,
    BackendRegistry,
    BackendUnavailableError,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    TurnResponse,
    default_registry,
)

# ───────────────────────────────────────────────────────────────
# PR 8: 新功能 — Discovery + Orchestration + Attach
# ───────────────────────────────────────────────────────────────

from .discovery import AgentRegistry, AgentRecord, PeerFinder
from .orchestration import Mission, MissionStep, MissionStore, MissionRunner, MissionStatus
from .membership import (
    JoinPolicy,
    RequestStatus,
    JoinRequest,
    MembershipRequest,
    TeamConfig,
    TeamRole,
    MembershipManager,
)
from .groups import (
    Announcement,
    AuditEvent,
    Channel,
    GroupManager,
    Message,
    MessageKind,
    Task,
    TaskStatus,
    TrustHint,
)
from .identity import (
    AgentID,
    AgentIdentity,
    crypto_available,
    default_identity_path,
    load_or_generate,
)
# A2A modules (originally proposed by @andy1868 in PR #3–#6, cherry-picked
# against current main; identity.py kept as the existing membership-gated
# version, these 4 new modules use the same AgentIdentity API and add zero
# third-party deps — all stdlib).
from .channel import ChannelMessage, TeamChannel
from .reputation import ReputationEntry, ReputationScore, ReputationManager
from .gossip import PeerInfo, GossipNode
from .marketplace import OrderStatus, TaskOrder, TaskMarketplace
from .attach import attach, TeamSession

__all__ = [
    # Facade（PR 1-7）
    "TeamAgent",
    "TeamMemoryManager",
    "SoulProvider",
    "UserModelProvider",
    "VectorProvider",
    "LedgerProvider",
    "CompressionPipeline",
    "CompressionStage",
    "EvoLoop",
    "EvoTrigger",
    "Reflector",
    "Verifier",
    "EvolutionGate",
    "SyncConfig",
    "LogCollector",
    "SkillLoader",
    "CentralAggregator",
    "Blackboard",
    "BlackboardEntry",
    "BlackboardProvider",
    "Scope",
    "render_kanban",
    "render_table",
    "AgentBackend",
    "BackendCapabilities",
    "BackendRegistry",
    "BackendUnavailableError",
    "SessionConfig",
    "SessionSummary",
    "TokenUsage",
    "TurnResponse",
    "default_registry",
    # PR 8 新增
    "AgentRegistry",
    "AgentRecord",
    "PeerFinder",
    "Mission",
    "MissionStep",
    "MissionStatus",
    "MissionStore",
    "MissionRunner",
    "attach",
    "TeamSession",
    # Membership（PR 9: 申请/审批加入）
    "JoinPolicy",
    "RequestStatus",
    "JoinRequest",
    "MembershipRequest",
    "TeamConfig",
    "TeamRole",
    "MembershipManager",
    # Local-first group layer: channels, messages, tasks, audit, trust
    "Announcement",
    "AuditEvent",
    "Channel",
    "GroupManager",
    "Message",
    "MessageKind",
    "Task",
    "TaskStatus",
    "TrustHint",
    # Identity: stable agent profile + optional Ed25519 signing
    "AgentID",
    "AgentIdentity",
    "crypto_available",
    "default_identity_path",
    "load_or_generate",
    # A2A modules cherry-picked from @andy1868 PR #3–#6 (stdlib only)
    "ChannelMessage",   # PR #3 signed A2A messaging
    "TeamChannel",
    "ReputationEntry",  # PR #4 subjective reputation
    "ReputationScore",
    "ReputationManager",
    "PeerInfo",         # PR #5 P2P gossip
    "GossipNode",
    "OrderStatus",      # PR #6 task marketplace
    "TaskOrder",
    "TaskMarketplace",
]
