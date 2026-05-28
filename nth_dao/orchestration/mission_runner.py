"""
MissionRunner   Agent


    1. find_work()      stepcapability  +
    2. claim()          step  Agent  Agent  claim
    3. execute()        backend
    4. complete() / handoff() / fail()


handoff
    handoff(step_id, to_agent_id, note)
      step.status = handed_off, assignee = to_agent_id
      to_agent_id  find_work()  step
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from .mission import MissionStep, MissionStatus, StepStatus
from .mission_store import MissionStore


@dataclass
class RunnerOutcome:
    """ claim+execute """
    success: bool
    mission_id: str
    step_id: str
    note: str = ""
    output: Optional[dict] = None


class MissionRunner:
    """ Agent """

    def __init__(
        self,
        store: MissionStore,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
    ):
        self.store = store
        self.agent_id = agent_id
        self.capabilities = capabilities or []

    #  1.

    def find_work(
        self,
        prefer_mission_id: Optional[str] = None,
        prefer_handoff_to_me: bool = True,
    ) -> Optional[tuple]:
        """
         Mission  step

            1.  handoff  Agent  step
            2. prefer_mission_id  Mission
            3. capability  +  TODO

        Returns:
            (mission, step)  None
        """
        relevant = self.store.list_for_agent(
            self.agent_id, self.capabilities, include_team=True
        )

        # handed_off  me
        if prefer_handoff_to_me:
            for m in relevant:
                for s in m.steps:
                    if (
                        s.status == StepStatus.HANDED_OFF.value
                        and s.assignee == self.agent_id
                    ):
                        return m, s

        #  mission
        if prefer_mission_id:
            m = self.store.get(prefer_mission_id)
            if m:
                actionable = m.next_actionable(self.capabilities)
                if actionable:
                    return m, actionable[0]

        #  TODO +
        for m in relevant:
            actionable = m.next_actionable(self.capabilities)
            if actionable:
                return m, actionable[0]

        return None

    #  2. claim

    def claim(self, mission_id: str, step_id: str) -> Optional[MissionStep]:
        """ step   active """
        return self.store.update_step(
            mission_id=mission_id,
            step_id=step_id,
            status=StepStatus.ACTIVE.value,
            assignee=self.agent_id,
            note=f"claimed by {self.agent_id} (caps={self.capabilities})",
            note_author=self.agent_id,
        )

    #  3.

    def complete(
        self,
        mission_id: str,
        step_id: str,
        output: Optional[dict] = None,
        note: str = "",
    ) -> RunnerOutcome:
        """ step"""
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
        """ step  Agent"""
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
        """ step"""
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

    #  4.

    def my_active_steps(self) -> List[tuple]:
        """ Agent  step (mission, step)"""
        out = []
        for m in self.store.list_active():
            for s in m.steps:
                if s.assignee == self.agent_id and s.status == StepStatus.ACTIVE.value:
                    out.append((m, s))
        return out
