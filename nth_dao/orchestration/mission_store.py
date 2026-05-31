"""
MissionStore — file-backed persistence for Mission objects.

Design:
    - Each mission lives in missions/<mission_id>.json
    - Writes use a tmp + rename atomic dance (see util.atomic_write_json)
    - Multi-process safety: try_claim() and update_step() acquire an
      InterProcessLock + thread-local RLock before reading + writing
    - Mission state is what gets Git-synced (via PR 5 git_sync) — that's how
      missions follow you across terminals
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .mission import Mission, MissionStatus, MissionStep, StepStatus
from ..util import (
    atomic_write_json,
    safe_load_json,
    safe_id as _safe_id,
    InterProcessLock,
)


# 进程内 RLock 加 fast path（避免对同一 mission 多个 thread 都去抢文件锁）
_LOCKS: Dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()


def _thread_lock_for(path: str) -> threading.RLock:
    with _LOCK_GUARD:
        if path not in _LOCKS:
            _LOCKS[path] = threading.RLock()
        return _LOCKS[path]


class ClaimConflict(Exception):
    """Step 已被别的 agent claim 或已超出可 claim 状态。"""


class MissionNotFound(Exception):
    pass


class StepNotFound(Exception):
    pass


class MissionStore:
    """Mission """

    DEFAULT_DIR = "missions"

    def __init__(self, root: Optional[str] = None):
        """
        Args:
            root: Mission  ./missions/ git_sync
        """
        self.root = Path(root) if root else Path(self.DEFAULT_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    #

    def save(self, mission: Mission) -> Path:
        """Atomically save a mission under thread and process locks."""
        path = self._path_for(mission.id)
        with _thread_lock_for(str(path)), InterProcessLock(path):
            return self._save_unlocked(mission)

    def create(self, mission: Mission) -> Path:
        """Create a new mission, failing if the id already exists."""
        path = self._path_for(mission.id)
        with _thread_lock_for(str(path)), InterProcessLock(path):
            if path.exists():
                raise FileExistsError(f"mission {mission.id} already exists")
            return self._save_unlocked(mission)

    def delete(self, mission_id: str) -> bool:
        path = self._path_for(mission_id)
        if not path.exists():
            return False
        with _thread_lock_for(str(path)), InterProcessLock(path):
            if path.exists():
                path.unlink()
        return True

    #

    def get(self, mission_id: str) -> Optional[Mission]:
        path = self._path_for(mission_id)
        data = safe_load_json(path, fallback=None)
        if data is None:
            return None
        try:
            return Mission.from_dict(data)
        except Exception:
            return None

    def list_all(self) -> List[Mission]:
        results = []
        for f in sorted(self.root.glob("*.json")):
            data = safe_load_json(f, fallback=None)
            if data is None:
                continue
            try:
                results.append(Mission.from_dict(data))
            except Exception:
                continue
        return results

    def list_active(self) -> List[Mission]:
        return [
            m for m in self.list_all()
            if m.status in (MissionStatus.ACTIVE.value, MissionStatus.PLANNING.value)
        ]

    def list_for_agent(
        self,
        agent_id: str,
        agent_capabilities: Optional[List[str]] = None,
        include_team: bool = True,
    ) -> List[Mission]:
        """
         Agent  Mission
        -  owner
        -  step.assignee=
        - shared scope  include_team
        -  claim  step  capability
        """
        all_missions = self.list_active()
        relevant = []
        for m in all_missions:
            if m.owner == agent_id:
                relevant.append(m)
                continue
            if any(s.assignee == agent_id for s in m.steps):
                relevant.append(m)
                continue
            if include_team and m.scope == "shared":
                actionable = m.next_actionable(agent_capabilities)
                if actionable:
                    relevant.append(m)
        return relevant

    #

    def update_step(
        self,
        mission_id: str,
        step_id: str,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        output: Optional[dict] = None,
        note: Optional[str] = None,
        note_author: str = "system",
        expect_status: Optional[str] = None,
        expect_assignee_in: Optional[List[str]] = None,
    ) -> Optional[MissionStep]:
        """更新 step + 检查 mission 终态。

        新增 compare-and-swap 前置条件参数：
            expect_status: 调用方期待 step 当前状态在此列表里（单值或 None=不检查）
            expect_assignee_in: 期待 step.assignee 在此列表里（"" 字符串代表"未占用"）
        前置不满足 → 抛 ClaimConflict（NOT silent overwrite）。

        Mission 状态机：
            - 所有 step DONE                  → COMPLETED
            - 至少一个 step FAILED 且无 actionable → FAILED
            - PLANNING 中有任意 step 离开 TODO → ACTIVE
        """
        path = self._path_for(mission_id)
        with _thread_lock_for(str(path)), InterProcessLock(path):
            mission = self.get(mission_id)
            if mission is None:
                return None
            step = mission.get_step(step_id)
            if step is None:
                return None

            # ── compare-and-swap 前置 ──
            if expect_status is not None and step.status != expect_status:
                raise ClaimConflict(
                    f"step {step_id} expected status={expect_status} "
                    f"but is {step.status}"
                )
            if expect_assignee_in is not None:
                # "" 在列表里 = 允许未分配；其它字符串 = 允许这个 agent
                if (step.assignee or "") not in expect_assignee_in:
                    raise ClaimConflict(
                        f"step {step_id} expected assignee in {expect_assignee_in} "
                        f"but is '{step.assignee}'"
                    )

            # ── apply 状态变更 ──
            # 关键修复：previous_assignees 在同一次调用里只 push 一次
            old_assignee = step.assignee
            new_assignee = assignee if assignee is not None else old_assignee
            if old_assignee and new_assignee and old_assignee != new_assignee:
                step.previous_assignees.append(old_assignee)

            if status is not None:
                step.status = status
                if status == StepStatus.DONE.value:
                    step.completed_at = datetime.now().isoformat()
            if assignee is not None:
                step.assignee = assignee
            if output is not None:
                step.output = output
            if note:
                step.add_note(note, note_author)

            # ── Mission 终态机 ──
            now_iso = datetime.now().isoformat()
            if mission.is_finished():
                mission.status = MissionStatus.COMPLETED.value
                mission.completed_at = now_iso
            elif _mission_is_dead(mission):
                # 有 FAILED step 且没有 actionable step → mission FAILED
                mission.status = MissionStatus.FAILED.value
                if not mission.completed_at:
                    mission.completed_at = now_iso
            elif mission.status == MissionStatus.PLANNING.value and any(
                s.status != StepStatus.TODO.value for s in mission.steps
            ):
                mission.status = MissionStatus.ACTIVE.value

            self._save_unlocked(mission)
            return step

    def try_claim(
        self,
        mission_id: str,
        step_id: str,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
    ) -> Optional[MissionStep]:
        """专门的原子 claim 入口 —— 失败抛 ClaimConflict.

        要求 step 当前在 TODO/HANDED_OFF/BLOCKED 之一，且 assignee 为空 或 == agent_id
        （后者支持 retry 同一 agent 重新 claim）。
        """
        allowed_status_when_unassigned = StepStatus.TODO.value
        # 用 update_step 的 CAS：但 update_step 一次只能 expect 一个 status；
        # 这里手动加锁后再做更细的检查。
        path = self._path_for(mission_id)
        with _thread_lock_for(str(path)), InterProcessLock(path):
            mission = self.get(mission_id)
            if mission is None:
                raise MissionNotFound(mission_id)
            step = mission.get_step(step_id)
            if step is None:
                raise StepNotFound(step_id)

            if step.status not in (
                StepStatus.TODO.value,
                StepStatus.HANDED_OFF.value,
                StepStatus.BLOCKED.value,
            ):
                raise ClaimConflict(
                    f"step {step_id} not claimable (status={step.status})"
                )

            if step.assignee and step.assignee != agent_id:
                # HANDED_OFF 给特定 agent 的情况：只允许那个 agent claim
                if step.status == StepStatus.HANDED_OFF.value:
                    raise ClaimConflict(
                        f"step {step_id} handed off to {step.assignee}, not {agent_id}"
                    )
                # 其它情况 assignee != "" 意味着已被 claim
                raise ClaimConflict(
                    f"step {step_id} already claimed by {step.assignee}"
                )

            # capability check
            if step.required_capabilities and capabilities is not None:
                if not set(step.required_capabilities).issubset(set(capabilities)):
                    raise ClaimConflict(
                        f"step {step_id} requires {step.required_capabilities}, "
                        f"agent only has {capabilities}"
                    )

            # 提交 claim
            old_assignee = step.assignee
            if old_assignee and old_assignee != agent_id:
                step.previous_assignees.append(old_assignee)
            step.status = StepStatus.ACTIVE.value
            step.assignee = agent_id
            step.add_note(
                f"claimed by {agent_id} (caps={capabilities or []})",
                author=agent_id,
            )

            if mission.status == MissionStatus.PLANNING.value:
                mission.status = MissionStatus.ACTIVE.value

            self._save_unlocked(mission)
            return step

    #

    def _path_for(self, mission_id: str) -> Path:
        return self.root / f"{_safe_id(mission_id)}.json"

    def _save_unlocked(self, mission: Mission) -> Path:
        path = self._path_for(mission.id)
        mission.updated_at = datetime.now().isoformat()
        atomic_write_json(path, mission.to_dict())
        return path


def _mission_is_dead(mission: Mission) -> bool:
    """有 FAILED step 且不再有 actionable 的 step → 整个 mission 死了。"""
    if not any(s.status == StepStatus.FAILED.value for s in mission.steps):
        return False
    # 不传 capability，宽容意义上看是否还有 step 能被推进
    return not mission.next_actionable(agent_capabilities=None)
