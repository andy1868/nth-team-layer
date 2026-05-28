"""
Agent 发现子系统

让每个加载 nth_dao 的 Agent 互相发现，无需中心服务器。

机制：
1. AgentRegistry — 每个 Agent 启动时在共享目录写心跳文件
                   后台定期更新 last_seen
                   Git 可同步（跨终端发现）
2. PeerFinder    — 按 capability / backend / status / scope 查询队友
                   自动过滤心跳超时的 Agent
"""

from .agent_registry import AgentRegistry, AgentRecord
from .peer_finder import PeerFinder

__all__ = ["AgentRegistry", "AgentRecord", "PeerFinder"]
