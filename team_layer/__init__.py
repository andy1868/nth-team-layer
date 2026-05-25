"""
Nth Team Layer — Hermes Agent 的团队协作增强层

架构：
- runtime.py: TeamAgent (继承 Hermes Agent) + 记忆管理
- memory_providers/: 4 个记忆 Provider（SOUL、用户模型、向量、账本）
- compression/: 5 层上下文压缩管线
- evolution/: EvoLoop 自进化引擎
- git_sync/: 多终端协同与日志管理
- sandbox/: 隔离执行环境

关键设计原则：
1. 零改 Hermes 原文件 — 所有功能在 team_layer/ 内实现
2. 继承 + 适配器模式 — TeamAgent 继承 Hermes Agent，不改原逻辑
3. Provider ABC — 对接 Hermes 的 Memory Provider 接口
4. Git SSOT — 分布式协同的唯一真实源
"""

__version__ = "1.0.0"
__author__ = "Nth Team Agent"

from .runtime import TeamAgent, TeamMemoryManager

__all__ = ["TeamAgent", "TeamMemoryManager"]
