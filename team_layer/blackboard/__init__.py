"""
Blackboard — 多 Agent 共享数据空间（黑板/看板）

经典模式（Hearsay-II, 1970s）：所有 Agent 通过共享的数据空间通信，
而非点对点调用。适合多 Agent 协作场景。

三层作用域：
    shared              — 全团队共享（Git 同步）
    group:<name>        — 子团队共享（Git 同步）
    private:<agent_id>  — 单 Agent 私有（本地）

数据风格：Append-only JSON Lines（与 PR 5 LogCollector 一致）
- 同一 entry_id 的更新会追加新版本，最新版本由 get() 自动取出
- 多终端并发写零冲突（hostname+timestamp 隔离）
- 完整历史保留，便于审计
"""

from .scope import Scope, ScopeKind
from .blackboard import Blackboard, BlackboardEntry
from .views import render_kanban, render_table
from .provider import BlackboardProvider

__all__ = [
    "Scope",
    "ScopeKind",
    "Blackboard",
    "BlackboardEntry",
    "render_kanban",
    "render_table",
    "BlackboardProvider",
]
