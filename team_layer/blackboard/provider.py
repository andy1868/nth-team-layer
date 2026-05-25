"""
BlackboardProvider — 把当前 Agent 的待办注入 system prompt

集成方式（在 team_entrypoint.py）：
    providers = [
        SoulProvider(...),
        UserModelProvider(...),
        VectorProvider(...),
        LedgerProvider(...),
        BlackboardProvider(agent_id="alice", groups=["frontend"]),  # 🆕
    ]

注入的内容（每次 prefetch）：
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
    """把当前 Agent 关心的 Blackboard 状态注入 system prompt"""

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
            agent_id: 当前 Agent 标识（用于 private scope 与作者过滤）
            blackboard_root: 黑板根目录
            groups: 此 Agent 所属的子团队列表
            max_per_scope: 每个作用域最多展示多少条
            include_done: 是否包含已完成的（默认只展示活跃的）
        """
        self.agent_id = agent_id
        self.bb = Blackboard(blackboard_root)
        self.groups = groups or []
        self.max_per_scope = max_per_scope
        self.include_done = include_done

    def initialize(self, context: dict) -> None:
        """无需特殊初始化（Blackboard 已是磁盘 lazy load）"""
        pass

    def prefetch(self, session_id: str) -> str:
        """生成黑板摘要块"""
        sections = []

        # 1. Private（仅本 Agent）
        private_scope = Scope.private(self.agent_id)
        private_entries = self.bb.list(scope=private_scope)
        private_active = self._filter_active(private_entries)
        if private_active:
            sections.append(self._render_section(
                f"My tasks ({private_scope})",
                private_active[: self.max_per_scope],
            ))

        # 2. Shared（全团队）
        shared_entries = self.bb.list(scope=Scope.shared())
        shared_active = self._filter_active(shared_entries)
        if shared_active:
            sections.append(self._render_section(
                "Team tasks (shared)",
                shared_active[: self.max_per_scope],
            ))

        # 3. Groups（每个子团队）
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

    # ─────────────────────────── 钩子 ───────────────────────────

    def on_pre_compress(self, compaction_hint: str) -> None:
        """压缩前 — Blackboard 是磁盘存储，无需保护内存"""
        pass

    def sync_turn(self, action: dict, result) -> None:
        """每轮 — 暂无被动同步（Agent 主动调 Blackboard.post/update）"""
        pass

    def on_session_end(self) -> None:
        """会话结束 — 无清理工作"""
        pass

    # ─────────────────────────── 内部 ───────────────────────────

    def _filter_active(self, entries):
        if self.include_done:
            return entries
        return [e for e in entries if e.status != "done"]

    @staticmethod
    def _render_section(title: str, entries) -> str:
        lines = [f"**{title}**"]
        for e in entries:
            short_topic = e.topic if len(e.topic) <= 50 else e.topic[:49] + "…"
            lines.append(f"  - [{e.status:7s}] {short_topic} (by {e.author})")
        return "\n".join(lines)
