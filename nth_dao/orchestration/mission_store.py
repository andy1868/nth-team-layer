"""
MissionStore  Mission


-  Mission  JSON missions/<mission_id>.json
-  .tmp  rename
-  Git  PR 5 git_sync  Mission
-
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .mission import Mission, MissionStatus, MissionStep, StepStatus


#  Mission
_LOCKS: Dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    with _LOCK_GUARD:
        if path not in _LOCKS:
            _LOCKS[path] = threading.RLock()
        return _LOCKS[path]


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
        """ .tmp  rename"""
        path = self._path_for(mission.id)
        lock = _lock_for(str(path))
        with lock:
            mission.updated_at = mission.updated_at  # noqa (touch)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(mission.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(path))
        return path

    def create(self, mission: Mission) -> Path:
        """ id  FileExistsError"""
        path = self._path_for(mission.id)
        if path.exists():
            raise FileExistsError(f"mission {mission.id} already exists")
        return self.save(mission)

    def delete(self, mission_id: str) -> bool:
        path = self._path_for(mission_id)
        if not path.exists():
            return False
        with _lock_for(str(path)):
            path.unlink()
        return True

    #

    def get(self, mission_id: str) -> Optional[Mission]:
        path = self._path_for(mission_id)
        if not path.exists():
            return None
        with _lock_for(str(path)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return Mission.from_dict(data)
            except Exception:
                return None

    def list_all(self) -> List[Mission]:
        results = []
        for f in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
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
    ) -> Optional[MissionStep]:
        """ step + """
        path = self._path_for(mission_id)
        with _lock_for(str(path)):
            mission = self.get(mission_id)
            if mission is None:
                return None
            step = mission.get_step(step_id)
            if step is None:
                return None

            if status is not None:
                #
                if assignee is not None and step.assignee and step.assignee != assignee:
                    step.previous_assignees.append(step.assignee)
                step.status = status
                if status == StepStatus.DONE.value:
                    from datetime import datetime
                    step.completed_at = datetime.now().isoformat()
            if assignee is not None:
                if step.assignee and step.assignee != assignee:
                    step.previous_assignees.append(step.assignee)
                step.assignee = assignee
            if output is not None:
                step.output = output
            if note:
                step.add_note(note, note_author)

            # Mission
            if mission.is_finished():
                mission.status = MissionStatus.COMPLETED.value
                from datetime import datetime
                mission.completed_at = datetime.now().isoformat()
            elif mission.status == MissionStatus.PLANNING.value and any(
                s.status != StepStatus.TODO.value for s in mission.steps
            ):
                mission.status = MissionStatus.ACTIVE.value

            self.save(mission)
            return step

    #

    def _path_for(self, mission_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "_-." else "-" for c in mission_id)
        return self.root / f"{safe}.json"
