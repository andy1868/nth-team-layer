"""
PeerFinder — 在注册表里查找队友

典型用法：
    finder = PeerFinder(registry)
    pythonistas = finder.find(capability="python", status="idle")
    teammate = finder.best_match(needed_capabilities=["python", "web"])
    finder.exclude_self(my_agent_id)  # 链式过滤

匹配评分：
    - 每命中一个 needed_capability 加 1 分
    - status=idle 加 0.5 分（优于 busy）
    - 同 group 加 0.3 分
    - 同 hostname 加 0.2 分（同机协作零延迟）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .agent_registry import AgentRecord, AgentRegistry


@dataclass
class MatchResult:
    """匹配结果（按 score 排序）"""
    record: AgentRecord
    score: float
    matched_capabilities: List[str]


class PeerFinder:
    """队友查找器"""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    # ─────────────────── 基础过滤 ───────────────────

    def find(
        self,
        capability: Optional[str] = None,
        backend_id: Optional[str] = None,
        group: Optional[str] = None,
        status: Optional[str] = None,
        only_alive: bool = True,
        exclude_agent_ids: Optional[List[str]] = None,
    ) -> List[AgentRecord]:
        """按单个条件过滤（多个条件可叠加）"""
        records = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if capability is not None:
            records = [r for r in records if capability in r.capabilities]
        if backend_id is not None:
            records = [r for r in records if r.backend_id == backend_id]
        if group is not None:
            records = [r for r in records if group in r.groups]
        if status is not None:
            records = [r for r in records if r.status == status]
        if exclude_agent_ids:
            excl = set(exclude_agent_ids)
            records = [r for r in records if r.agent_id not in excl]
        return records

    def find_all(
        self,
        capabilities: Optional[List[str]] = None,
        backend_ids: Optional[List[str]] = None,
        only_alive: bool = True,
    ) -> List[AgentRecord]:
        """需要满足多个 capability（AND 关系）"""
        records = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if capabilities:
            req = set(capabilities)
            records = [r for r in records if req.issubset(set(r.capabilities))]
        if backend_ids:
            allowed = set(backend_ids)
            records = [r for r in records if r.backend_id in allowed]
        return records

    # ─────────────────── 加权匹配 ───────────────────

    def best_match(
        self,
        needed_capabilities: List[str],
        prefer_idle: bool = True,
        prefer_group: Optional[str] = None,
        prefer_hostname: Optional[str] = None,
        exclude_agent_ids: Optional[List[str]] = None,
        only_alive: bool = True,
    ) -> Optional[MatchResult]:
        """返回最佳匹配（按 score 最高）"""
        results = self.rank(
            needed_capabilities=needed_capabilities,
            prefer_idle=prefer_idle,
            prefer_group=prefer_group,
            prefer_hostname=prefer_hostname,
            exclude_agent_ids=exclude_agent_ids,
            only_alive=only_alive,
        )
        return results[0] if results else None

    def rank(
        self,
        needed_capabilities: List[str],
        prefer_idle: bool = True,
        prefer_group: Optional[str] = None,
        prefer_hostname: Optional[str] = None,
        exclude_agent_ids: Optional[List[str]] = None,
        only_alive: bool = True,
        min_score: float = 0.5,
    ) -> List[MatchResult]:
        """对所有候选打分排序"""
        candidates = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if exclude_agent_ids:
            excl = set(exclude_agent_ids)
            candidates = [r for r in candidates if r.agent_id not in excl]

        results = []
        for r in candidates:
            matched = [c for c in needed_capabilities if c in r.capabilities]
            score = float(len(matched))

            if prefer_idle and r.status == "idle":
                score += 0.5
            if prefer_group and prefer_group in r.groups:
                score += 0.3
            if prefer_hostname and r.hostname == prefer_hostname:
                score += 0.2

            if score >= min_score:
                results.append(MatchResult(
                    record=r, score=score, matched_capabilities=matched,
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    # ─────────────────── 便利方法 ───────────────────

    def count_alive(self) -> int:
        return len(self.registry.list_alive())

    def capability_index(self) -> dict:
        """返回 {capability: [agent_id, ...]} 索引"""
        idx: dict = {}
        for r in self.registry.list_alive():
            for cap in r.capabilities:
                idx.setdefault(cap, []).append(r.agent_id)
        return idx

    def summary_table(self) -> str:
        """ASCII 表格列出所有 alive Agent"""
        records = self.registry.list_alive()
        if not records:
            return "(no agents online)"
        lines = [
            f"{'agent_id':25s} | {'backend':12s} | {'status':8s} | {'capabilities':30s} | last_seen",
            "-" * 100,
        ]
        for r in records:
            caps = ",".join(r.capabilities[:3])
            if len(r.capabilities) > 3:
                caps += f"+{len(r.capabilities) - 3}"
            lines.append(
                f"{r.agent_id:25s} | {r.backend_id:12s} | {r.status:8s} | {caps:30s} | {r.last_seen[:19]}"
            )
        return "\n".join(lines)
