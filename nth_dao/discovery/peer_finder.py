"""
PeerFinder


    finder = PeerFinder(registry)
    pythonistas = finder.find(capability="python", status="idle")
    teammate = finder.best_match(needed_capabilities=["python", "web"])
    finder.exclude_self(my_agent_id)  #


    -  needed_capability  1
    - status=idle  0.5  busy
    -  group  0.3
    -  hostname  0.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from .agent_registry import AgentRecord, AgentRegistry

# ── search scoring constants ──
EXACT_MATCH_WEIGHT = 3.0
PREFIX_MATCH_WEIGHT = 1.5
SUBSTRING_MATCH_WEIGHT = 0.8
IDLE_BONUS = 0.5
GROUP_BONUS = 0.3
HOSTNAME_BONUS = 0.2
ACCEPTING_TASKS_BONUS = 0.5

ComplementDirection = Literal["bidirectional", "incoming", "outgoing"]


@dataclass
class MatchResult:
    """ score """
    record: AgentRecord
    score: float
    matched_capabilities: List[str]
    match_details: Dict[str, Any] = field(default_factory=dict)


class PeerFinder:
    """"""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    #

    def find(
        self,
        capability: Optional[str] = None,
        backend_id: Optional[str] = None,
        group: Optional[str] = None,
        status: Optional[str] = None,
        only_alive: bool = True,
        exclude_agent_ids: Optional[List[str]] = None,
    ) -> List[AgentRecord]:
        """"""
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
        """ capabilityAND """
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

    #

    def best_match(
        self,
        needed_capabilities: List[str],
        prefer_idle: bool = True,
        prefer_group: Optional[str] = None,
        prefer_hostname: Optional[str] = None,
        exclude_agent_ids: Optional[List[str]] = None,
        only_alive: bool = True,
        min_match: int = 1,
    ) -> Optional[MatchResult]:
        """Return best match; min_match=N requires at least N capability hits."""
        results = self.rank(
            needed_capabilities=needed_capabilities,
            prefer_idle=prefer_idle,
            prefer_group=prefer_group,
            prefer_hostname=prefer_hostname,
            exclude_agent_ids=exclude_agent_ids,
            only_alive=only_alive,
            min_match=min_match,
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
        min_match: int = 1,
    ) -> List[MatchResult]:
        """Results sorted by score descending.

        Args:
            min_match: Minimum number of capability hits required.
                       Default 1 (at least 1 cap must match).
                       Pass 0 to match any alive agent — previously
                       min_score=0.5 returned agents with 0 matched caps
                       but idle status, which was the H-6 pitfall.
        """
        candidates = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if exclude_agent_ids:
            excl = set(exclude_agent_ids)
            candidates = [r for r in candidates if r.agent_id not in excl]

        results = []
        for r in candidates:
            matched = [c for c in needed_capabilities if c in r.capabilities]
            if len(matched) < min_match:
                continue
            score = float(len(matched))

            if prefer_idle and r.status == "idle":
                score += IDLE_BONUS
            if prefer_group and prefer_group in r.groups:
                score += GROUP_BONUS
            if prefer_hostname and r.hostname == prefer_hostname:
                score += HOSTNAME_BONUS

            results.append(MatchResult(
                record=r, score=score, matched_capabilities=matched,
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    #

    def search(
        self,
        query: str,
        *,
        fields: Optional[List[str]] = None,
        limit: int = 10,
        min_score: float = 0.5,
        only_alive: bool = True,
        exclude_agent_ids: Optional[List[str]] = None,
    ) -> List["MatchResult"]:
        """Fuzzy-search agents by agent_id / label / capabilities / groups.

        Args:
            query: Search term (case-insensitive). Empty string returns [].
            fields: Field subset to search.  Default ["agent_id", "label",
                    "capabilities", "groups"].
            limit: Max results to return.
            min_score: Drop results below this score.
            only_alive: Only search alive agents.
            exclude_agent_ids: Exclude specific agent_ids (e.g. self).

        Scoring (per-field, cumulative):
            - Exact match       +3.0
            - Prefix match      +1.5
            - Substring match   +0.8
            - Multi-field hits accumulate; status=idle adds +0.5

        Returns:
            MatchResult list sorted by score descending.
            ``matched_capabilities`` carries the ``(field, value)`` pairs
            that matched (note: reuses the field name as a carrier).
        """
        if not query:
            return []
        q = query.lower().strip()
        if not q:
            return []

        all_fields = ["agent_id", "label", "capabilities", "groups"]
        active_fields = [f for f in (fields or all_fields) if f in all_fields]

        candidates = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if exclude_agent_ids:
            excl = set(exclude_agent_ids)
            candidates = [r for r in candidates if r.agent_id not in excl]

        results: List[MatchResult] = []
        for r in candidates:
            score, matched_pairs = _score_record(r, q, active_fields)
            if score < min_score:
                continue
            if r.status == "idle":
                score += IDLE_BONUS
            # MatchResult 复用既有结构：matched_capabilities 改装成 "field:value" 列表
            results.append(MatchResult(
                record=r,
                score=score,
                matched_capabilities=matched_pairs,
            ))

        results.sort(key=lambda m: m.score, reverse=True)
        return results[:limit]

    def count_alive(self) -> int:
        return len(self.registry.list_alive())

    def capability_index(self) -> dict:
        """ {capability: [agent_id, ...]} """
        idx: dict = {}
        for r in self.registry.list_alive():
            for cap in r.capabilities:
                idx.setdefault(cap, []).append(r.agent_id)
        return idx

    def find_complements(
        self,
        agent_id: str,
        direction: ComplementDirection = "bidirectional",
    ) -> List[MatchResult]:
        """Find agents whose capabilities complement *agent_id*'s needs.

        Matching is purely based on ``seeking`` x ``capabilities``:

        * **incoming**: other agent HAS a capability that *agent_id* is SEEKING.
          (Who can help me?)
        * **outgoing**: *agent_id* HAS a capability that the other agent is SEEKING.
          (Who can I help?)
        * **bidirectional** (default): either direction matches.

        ``accepting_tasks`` gives a score bonus (+ACCEPTING_TASKS_BONUS)
        but does NOT filter results.
        ``available_for`` is metadata for consumers — it is NOT used for
        complement filtering (it describes accepted action types, not
        capabilities).
        """
        if direction not in ("bidirectional", "incoming", "outgoing"):
            raise ValueError(
                f"direction must be 'bidirectional', 'incoming', or 'outgoing', "
                f"got {direction!r}"
            )
        all_agents = self.registry.list_alive()
        my_record = None
        for r in all_agents:
            if r.agent_id == agent_id:
                my_record = r
                break
        if my_record is None:
            return []

        my_caps = set(my_record.capabilities)
        my_seeking = set(my_record.seeking)

        results: List[MatchResult] = []
        for r in all_agents:
            if r.agent_id == agent_id:
                continue

            other_caps = r.capabilities  # list, iterated once per intersection below
            other_seeking = r.seeking

            # Directional match sets (lazy intersection avoids per-iteration
            # set() construction — O(n²) in agent count)
            skills_they_offer = [c for c in other_caps if c in my_seeking]
            skills_i_offer = [c for c in other_seeking if c in my_caps]

            # Collect matched capabilities based on direction
            if direction == "incoming":
                matched = skills_they_offer
            elif direction == "outgoing":
                matched = skills_i_offer
            else:  # bidirectional
                matched = skills_they_offer + skills_i_offer

            if not matched:
                continue

            score = float(len(matched))
            if r.accepting_tasks:
                score += ACCEPTING_TASKS_BONUS

            # M2: include match direction (non-mutating)
            result = MatchResult(
                record=r,
                score=score,
                matched_capabilities=matched,
                match_details={
                    "they_have": skills_they_offer,
                    "i_have": skills_i_offer,
                },
            )

            results.append(result)

        results.sort(key=lambda m: m.score, reverse=True)
        return results

    def summary_table(self) -> str:  # noqa: E303
        # placeholder anchor — actual definition below
        return _summary_table_impl(self.registry)


# ───────────────────────── fuzzy-search helpers ─────────────────────────


# Score weights — tuned so:
#   "alic" prefix-matching agent_id "alice" → score ≈ 1.5 + maybe 1.5 on label
#   "alice" exact agent_id              → score ≈ 3.0 + label 3.0 = 6.0
def _field_score(haystack: str, needle: str) -> float:
    """Single-field substring/prefix/exact score (case-insensitive)."""
    if not haystack:
        return 0.0
    hay = haystack.lower()
    if hay == needle:
        return EXACT_MATCH_WEIGHT
    if hay.startswith(needle):
        return PREFIX_MATCH_WEIGHT
    if needle in hay:
        return SUBSTRING_MATCH_WEIGHT
    return 0.0


def _extract_label(record) -> str:
    """Pull the display label from registry metadata (set by attach.py)."""
    meta = record.metadata or {}
    ident = meta.get("identity") if isinstance(meta, dict) else None
    if isinstance(ident, dict):
        return str(ident.get("label", "") or "")
    return str(meta.get("label", "") or "") if isinstance(meta, dict) else ""


def _score_record(record, q: str, fields):
    """Returns (score, ["field:matched-value", ...]) for one record/query."""
    score = 0.0
    matched = []
    if "agent_id" in fields:
        s = _field_score(record.agent_id, q)
        if s > 0:
            score += s
            matched.append(f"agent_id:{record.agent_id}")
    if "label" in fields:
        label = _extract_label(record)
        if label:
            s = _field_score(label, q)
            if s > 0:
                score += s
                matched.append(f"label:{label}")
    if "capabilities" in fields:
        for cap in record.capabilities:
            s = _field_score(cap, q)
            if s > 0:
                score += s
                matched.append(f"capability:{cap}")
                break  # one capability match is enough for ranking
    if "groups" in fields:
        for g in record.groups:
            s = _field_score(g, q)
            if s > 0:
                score += s
                matched.append(f"group:{g}")
                break
    return score, matched


def _summary_table_impl(registry):
    """ASCII table of alive agents."""
    records = registry.list_alive()
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
