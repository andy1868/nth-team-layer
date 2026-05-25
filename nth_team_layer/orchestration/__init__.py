"""
Orchestration — 超长期任务接力子系统

设计思路：
- 一个 Mission = 一个长期目标（如"上线支付 v2"），可能跨数天 / 多 Agent / 多终端
- 每个 Mission 由 N 个 Step 组成（todo → active → done / failed / handed_off）
- MissionStore 持久化所有 Mission（Git 同步）
- MissionRunner 协调"接力"：Agent claim 一个 step、做完、handoff 或 complete

为什么独立于 Blackboard：
- Blackboard 是"自由记录"（任何条目）
- Mission 是"有结构的长期任务"（步骤、依赖、handoff、deadline）
- Mission 可以在 Blackboard 派生视图（看板上看到 Mission 进度）
"""

from .mission import Mission, MissionStatus, MissionStep, StepStatus
from .mission_store import MissionStore
from .mission_runner import MissionRunner, RunnerOutcome

__all__ = [
    "Mission",
    "MissionStatus",
    "MissionStep",
    "StepStatus",
    "MissionStore",
    "MissionRunner",
    "RunnerOutcome",
]
