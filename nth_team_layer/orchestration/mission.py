"""
Mission & MissionStep — 数据模型

Mission 状态机：
    planning → active → completed
                     ↘ failed
                     ↘ paused

Step 状态机：
    todo → claimed → active → done
                                ↘ failed
                                ↘ handed_off  (交给另一个 Agent)
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MissionStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    TODO = "todo"
    CLAIMED = "claimed"      # 被某 Agent 认领，但还没开始
    ACTIVE = "active"        # 该 Agent 正在执行
    DONE = "done"
    FAILED = "failed"
    HANDED_OFF = "handed_off"  # 当前 Agent 主动交给另一个 Agent
    BLOCKED = "blocked"


@dataclass
class MissionStep:
    """长期 Mission 的单个步骤"""
    id: str
    description: str
    status: str = StepStatus.TODO.value

    # 需求与契约
    required_capabilities: List[str] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None

    # 依赖与接力
    depends_on: List[str] = field(default_factory=list)   # 其他 step id
    assignee: Optional[str] = None                         # 当前 owner agent_id
    previous_assignees: List[str] = field(default_factory=list)  # 接力历史

    # 时间戳
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None

    # 失败原因 / 备注
    notes: List[str] = field(default_factory=list)

    def add_note(self, note: str, author: str = "system") -> None:
        ts = datetime.now().isoformat()
        self.notes.append(f"[{ts[:19]}] {author}: {note}")
        self.updated_at = ts

    def can_start(self, completed_step_ids: set) -> bool:
        """依赖的 step 都完成了吗？"""
        return set(self.depends_on).issubset(completed_step_ids)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            StepStatus.DONE.value,
            StepStatus.FAILED.value,
            StepStatus.HANDED_OFF.value,
        )

    @property
    def is_open(self) -> bool:
        """是否还能被 claim"""
        return self.status in (StepStatus.TODO.value, StepStatus.HANDED_OFF.value, StepStatus.BLOCKED.value)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MissionStep":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class Mission:
    """超长期任务"""
    id: str
    title: str
    goal: str
    status: str = MissionStatus.PLANNING.value
    owner: str = ""                          # 发起 Agent
    scope: str = "shared"                    # 与 Blackboard scope 一致（共享 / group:X / private:X）

    steps: List[MissionStep] = field(default_factory=list)

    # 元数据
    deadline: Optional[str] = None
    priority: str = "normal"  # low / normal / high / critical
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 时间戳
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None

    @classmethod
    def new(
        cls,
        title: str,
        goal: str,
        owner: str,
        scope: str = "shared",
        steps: Optional[List[dict]] = None,
        **kwargs,
    ) -> "Mission":
        """工厂：创建新 Mission，steps 可以传 dict 列表"""
        m = cls(
            id=uuid.uuid4().hex[:12],
            title=title,
            goal=goal,
            owner=owner,
            scope=scope,
            **kwargs,
        )
        if steps:
            for s in steps:
                step = MissionStep(
                    id=s.get("id") or uuid.uuid4().hex[:8],
                    description=s["description"],
                    required_capabilities=s.get("required_capabilities", []),
                    depends_on=s.get("depends_on", []),
                    inputs=s.get("inputs", {}),
                )
                m.steps.append(step)
        return m

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items() if k != "steps"},
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Mission":
        fields = {f for f in cls.__dataclass_fields__}
        steps_data = data.pop("steps", [])
        m = cls(**{k: v for k, v in data.items() if k in fields})
        m.steps = [MissionStep.from_dict(s) for s in steps_data]
        return m

    # ─── 状态查询 ───

    def get_step(self, step_id: str) -> Optional[MissionStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def completed_step_ids(self) -> set:
        return {s.id for s in self.steps if s.status == StepStatus.DONE.value}

    def next_actionable(self, agent_capabilities: Optional[List[str]] = None) -> List[MissionStep]:
        """返回当前可以被 claim 的 step（依赖已完成 + capability 匹配）"""
        done_ids = self.completed_step_ids()
        candidates = []
        for s in self.steps:
            if not s.is_open:
                continue
            if not s.can_start(done_ids):
                continue
            # capability 检查
            if agent_capabilities is not None and s.required_capabilities:
                if not set(s.required_capabilities).issubset(set(agent_capabilities)):
                    continue
            candidates.append(s)
        return candidates

    def progress(self) -> dict:
        """整体进度统计"""
        total = len(self.steps)
        if total == 0:
            return {"total": 0, "done": 0, "active": 0, "open": 0, "failed": 0, "percent": 0.0}
        done = sum(1 for s in self.steps if s.status == StepStatus.DONE.value)
        active = sum(1 for s in self.steps if s.status == StepStatus.ACTIVE.value)
        open_ = sum(1 for s in self.steps if s.is_open)
        failed = sum(1 for s in self.steps if s.status == StepStatus.FAILED.value)
        return {
            "total": total,
            "done": done,
            "active": active,
            "open": open_,
            "failed": failed,
            "percent": round(done / total * 100, 1),
        }

    def is_finished(self) -> bool:
        if not self.steps:
            return False
        return all(s.status == StepStatus.DONE.value for s in self.steps)

    def short(self) -> str:
        p = self.progress()
        return (
            f"[{self.status:9s}] {self.id} '{self.title}' — "
            f"{p['done']}/{p['total']} done ({p['percent']}%), "
            f"{p['active']} active, {p['failed']} failed"
        )
