"""
BlackboardProvider   Agent  system prompt

 team_entrypoint.py
    providers = [
        SoulProvider(...),
        UserModelProvider(...),
        VectorProvider(...),
        LedgerProvider(...),
        BlackboardProvider(agent_id="alice", groups=["frontend"]),  #
    ]

 prefetch
    ## Blackboard
    My active tasks (private:alice):
      - [doing  ] Refactor auth module
    Team tasks (shared):
      - [todo   ] Add E2E tests
      - [doing  ] Set up CI (bob)
    Group: frontend (group:frontend):
      - [todo   ] Migrate to React 19
"""

from typing import List, Optional

from ..runtime import MemoryProviderABC
from .blackboard import Blackboard
from .scope import Scope


class BlackboardProvider(MemoryProviderABC):
    """ Agent  Blackboard  system prompt"""

    def __init__(
        self,
        agent_id: str,
        blackboard_root: str = "blackboard",
        groups: Optional[List[str]] = None,
        max_per_scope: int = 5,
        include_done: bool = False,
    ):
        """
        Args:
            agent_id:  Agent  private scope
            blackboard_root:
            groups:  Agent
            max_per_scope:
            include_done:
        """
        self.agent_id = agent_id
        self.bb = Blackboard(blackboard_root)
        self.groups = groups or []
        self.max_per_scope = max_per_scope
        self.include_done = include_done

    def initialize(self, context: dict) -> None:
        """Blackboard  lazy load"""
        pass

    def prefetch(self, session_id: str) -> str:
        """"""
        sections = []

        # 1. Private Agent
        private_scope = Scope.private(self.agent_id)
        private_entries = self.bb.list(scope=private_scope)
        private_active = self._filter_active(private_entries)
        if private_active:
            sections.append(self._render_section(
                f"My tasks ({private_scope})",
                private_active[: self.max_per_scope],
            ))

        # 2. Shared
        shared_entries = self.bb.list(scope=Scope.shared())
        shared_active = self._filter_active(shared_entries)
        if shared_active:
            sections.append(self._render_section(
                "Team tasks (shared)",
                shared_active[: self.max_per_scope],
            ))

        # 3. Groups
        for group in self.groups:
            group_scope = Scope.group(group)
            group_entries = self.bb.list(scope=group_scope)
            group_active = self._filter_active(group_entries)
            if group_active:
                sections.append(self._render_section(
                    f"Group: {group} ({group_scope})",
                    group_active[: self.max_per_scope],
                ))

        if not sections:
            return "## Blackboard\n(no active tasks)"

        return "## Blackboard\n" + "\n\n".join(sections)

    #

    def on_pre_compress(self, compaction_hint: str) -> None:
        """  Blackboard """
        pass

    def sync_turn(self, action: dict, result) -> None:
        """  Agent  Blackboard.post/update"""
        pass

    def on_session_end(self) -> None:
        """  """
        pass

    #

    def _filter_active(self, entries):
        if self.include_done:
            return entries
        return [e for e in entries if e.status != "done"]

    @staticmethod
    def _render_section(title: str, entries) -> str:
        lines = [f"**{title}**"]
        for e in entries:
            short_topic = e.topic if len(e.topic) <= 50 else e.topic[:49] + ""
            lines.append(f"  - [{e.status:7s}] {short_topic} (by {e.author})")
        return "\n".join(lines)
