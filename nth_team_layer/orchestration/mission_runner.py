"""
MissionRunner — 跨 Agent 接力执行器

工作流：
    1. find_work()    — 找到一个可执行的 step（capability 匹配 + 依赖完成）
    2. claim()        — 把这个 step 标记为本 Agent 占用（防止其他 Agent 也 claim）
    3. execute()      — 用 backend 执行（或交给业务代码执行）
    4. complete() / handoff() / fail()
                      — 收尾，落盘，给下一棒留下接力上下文

接力（handoff）的关键：
    handoff(step_id, to_agent_id, note)
    → 把 step.status = handed_off, assignee = to_agent_id
    → 下次 to_agent_id 调 find_work() 时优先看到此 step
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from .mission import MissionStep, MissionStatus, StepStatus
from .mission_store import MissionStore


@dataclass
class RunnerOutcome:
    """单次 claim+execute 周期的结果"""
    success: bool
    mission_id: str
    step_id: str
    note: str = ""
    output: Optional[dict] = None


class MissionRunner:
    """跨 Agent 接力执行器"""

    def __init__(
        self,
        store: MissionStore,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
    ):
        self.store = store
        self.agent_id = agent_id
        self.capabilities = capabilities or []

    # ─── 1. 发现工作 ───

    def find_work(
        self,
        prefer_mission_id: Optional[str] = None,
        prefer_handoff_to_me: bool = True,
    ) -> Optional[tuple]:
        """
        在所有相关 Mission 里找一个可执行的 step。
        优先级：
            1. 显式 handoff 给本 Agent 的 step
            2. prefer_mission_id 指定的 Mission
            3. capability 完全匹配 + 全新 TODO

        Returns:
            (mission, step) 或 None
        """
        relevant = self.store.list_for_agent(
            self.agent_id, self.capabilities, include_team=True
        )

        # 优先：handed_off → me
        if prefer_handoff_to_me:
            for m in relevant:
                for s in m.steps:
                    if (
                        s.status == StepStatus.HANDED_OFF.value
                        and s.assignee == self.agent_id
                    ):
                        return m, s

        # 优先：指定 mission
        if prefer_mission_id:
            m = self.store.get(prefer_mission_id)
            if m:
                actionable = m.next_actionable(self.capabilities)
                if actionable:
                    return m, actionable[0]

        # 普通：任何 TODO + 依赖满足
        for m in relevant:
            actionable = m.next_actionable(self.capabilities)
            if actionable:
                return m, actionable[0]

        return None

    # ─── 2. claim ───

    def claim(self, mission_id: str, step_id: str) -> Optional[MissionStep]:
        """认领一个 step → 标记 active 状态"""
        return self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.ACTIVE.value,
            assignee=self.agent_id,
            note=f"claimed by {self.agent_id} (caps={self.capabilities})",
            note_author=self.agent_id,
        )

    # ─── 3. 收尾 ───

    def complete(
        self,
        mission_id: str,
        step_id: str,
        output: Optional[dict] = None,
        note: str = "",
    ) -> RunnerOutcome:
        """完成一个 step"""
        step = self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.DONE.value,
            output=output,
            note=note or "completed",
            note_author=self.agent_id,
        )
        return RunnerOutcome(
            success=step is not None,
            mission_id=mission_id,
            step_id=step_id,
            note=note,
            output=output,
        )

    def handoff(
        self,
        mission_id: str,
        step_id: str,
        to_agent_id: str,
        note: str = "",
    ) -> RunnerOutcome:
        """把一个 step 交给另一个 Agent"""
        handoff_note = note or f"handed off from {self.agent_id} to {to_agent_id}"
        step = self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.HANDED_OFF.value,
            assignee=to_agent_id,
            note=handoff_note,
            note_author=self.agent_id,
        )
        return RunnerOutcome(
            success=step is not None,
            mission_id=mission_id,
            step_id=step_id,
            note=handoff_note,
        )

    def fail(
        self,
        mission_id: str,
        step_id: str,
        reason: str,
    ) -> RunnerOutcome:
        step = self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.FAILED.value,
            note=f"FAILED: {reason}",
            note_author=self.agent_id,
        )
        return RunnerOutcome(
            success=False,
            mission_id=mission_id,
            step_id=step_id,
            note=reason,
        )

    def block(self, mission_id: str, step_id: str, reason: str) -> RunnerOutcome:
        """暂时挂起一个 step（等待外部条件）"""
        step = self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.BLOCKED.value,
            note=f"BLOCKED: {reason}",
            note_author=self.agent_id,
        )
        return RunnerOutcome(
            success=step is not None,
            mission_id=mission_id,
            step_id=step_id,
            note=reason,
        )

    # ─── 4. 报告 ───

    def my_active_steps(self) -> List[tuple]:
        """当前本 Agent 正在执行的 step (mission, step)"""
        out = []
        for m in self.store.list_active():
            for s in m.steps:
                if s.assignee == self.agent_id and s.status == StepStatus.ACTIVE.value:
                    out.append((m, s))
        return out
