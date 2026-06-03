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

from dataclasses import dataclass
from typing import List, Optional

from .agent_registry import AgentRecord, AgentRegistry, CapacityStatus


@dataclass
class MatchResult:
    """ score """
    record: AgentRecord
    score: float
    matched_capabilities: List[str]


class PeerFinder:
    """Capability- and capacity-aware peer search over the agent registry.

    Scoring weights — tuneable constants (override via subclass or
    monkey-patch for domain-specific routing preferences):

    - ``SCORE_PER_CAPABILITY`` — base score for each matched capability
    - ``SCORE_STATUS_IDLE`` — bonus when ``status == "idle"`` (legacy;
      prefer ``SCORE_QUEUE_EMPTY`` instead)
    - ``SCORE_QUEUE_EMPTY`` — bonus when ``queue_depth == 0``
    - ``SCORE_BUSY_PROPORTION`` — multiplier for ``free_slots / max``
    - ``SCORE_WAIT_PENALTY_CAP`` — max penalty for long wait times
    - ``SCORE_WAIT_DIVISOR_SECS`` — wait seconds before penalty saturates
    - ``SCORE_GROUP_MATCH`` — bonus for group membership
    - ``SCORE_HOSTNAME_MATCH`` — bonus for same-host routing
    """

    SCORE_PER_CAPABILITY:      float = 1.0
    SCORE_STATUS_IDLE:          float = 0.5   # deprecated — prefer SCORE_QUEUE_EMPTY
    SCORE_QUEUE_EMPTY:          float = 1.0
    SCORE_BUSY_PROPORTION:      float = 0.5
    SCORE_WAIT_PENALTY_CAP:     float = 0.5
    SCORE_WAIT_DIVISOR_SECS:    float = 120.0
    SCORE_GROUP_MATCH:          float = 0.3
    SCORE_HOSTNAME_MATCH:       float = 0.2

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
        prefer_available: bool = True,
        prefer_group: Optional[str] = None,
        prefer_hostname: Optional[str] = None,
        exclude_agent_ids: Optional[List[str]] = None,
        only_alive: bool = True,
        min_match: int = 1,
    ) -> Optional[MatchResult]:
        """Return the single best-scored match."""
        results = self.rank(
            needed_capabilities=needed_capabilities,
            prefer_idle=prefer_idle,
            prefer_available=prefer_available,
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
        prefer_available: bool = True,
        prefer_group: Optional[str] = None,
        prefer_hostname: Optional[str] = None,
        exclude_agent_ids: Optional[List[str]] = None,
        only_alive: bool = True,
        min_match: int = 1,
    ) -> List[MatchResult]:
        """Score and sort candidates by capability match + capacity.

        Args:
            min_match: Minimum number of matched capabilities (default 1).
            prefer_idle: Legacy bonus for ``status == "idle"``.  When
                ``prefer_available`` is also True, this is suppressed
                to avoid double-counting (both reward an empty queue).
            prefer_available: When True, exclude OVERLOADED agents and
                boost low-queue-depth agents. When False, include
                everyone regardless of capacity.
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

            # Exclude overloaded agents when capacity-aware routing is on
            if prefer_available and r.capacity_status == CapacityStatus.OVERLOADED:
                continue

            score = float(len(matched)) * self.SCORE_PER_CAPABILITY

            if prefer_available:
                # Capacity bonus: lower queue = higher score.
                # When prefer_available is on, suppress the legacy
                # prefer_idle bonus to avoid double-counting.
                if r.queue_depth == 0:
                    score += self.SCORE_QUEUE_EMPTY
                elif r.max_concurrent_tasks > 0:
                    free_slots = r.max_concurrent_tasks - r.queue_depth
                    score += max(0.0, self.SCORE_BUSY_PROPORTION * free_slots / r.max_concurrent_tasks)
                if r.estimated_wait_seconds > 0:
                    score -= min(self.SCORE_WAIT_PENALTY_CAP,
                                 r.estimated_wait_seconds / self.SCORE_WAIT_DIVISOR_SECS)
            elif prefer_idle and r.status == "idle":
                score += self.SCORE_STATUS_IDLE

            if prefer_group and prefer_group in r.groups:
                score += self.SCORE_GROUP_MATCH
            if prefer_hostname and r.hostname == prefer_hostname:
                score += self.SCORE_HOSTNAME_MATCH

            results.append(MatchResult(
                record=r, score=score, matched_capabilities=matched,
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    def find_available(
        self,
        capability: Optional[str] = None,
        only_alive: bool = True,
        exclude_agent_ids: Optional[List[str]] = None,
    ) -> List[AgentRecord]:
        """Return agents that can accept new work — not OFFLINE and not
        OVERLOADED.  Useful for orchestrators that need to route a task
        to ANY available agent with a given capability.

        When ``only_alive=True`` (default), OFFLINE agents are already
        excluded by ``list_alive()``; the OFFLINE filter here only
        matters when ``only_alive=False``.
        """
        candidates = (
            self.registry.list_alive() if only_alive else self.registry.list_all()
        )
        if exclude_agent_ids:
            excl = set(exclude_agent_ids)
            candidates = [r for r in candidates if r.agent_id not in excl]
        if capability is not None:
            candidates = [r for r in candidates if capability in r.capabilities]
        return [
            r for r in candidates
            if r.capacity_status not in (CapacityStatus.OFFLINE, CapacityStatus.OVERLOADED)
        ]

    #

    def search(
        self,
        query: str,
        *,
        fields: Optional[List[str]] = None,
        limit: int = 10,
        min_score: float = 0.5,
        only_alive: bool = True,
        prefer_available: bool = True,
        exclude_agent_ids: Optional[List[str]] = None,
    ) -> List["MatchResult"]:
        """Fuzzy search across agent_id / label / capabilities / groups.

        Args:
            query: Search term (case-insensitive). Empty → [].
            fields: Fields to search; default all four.
            limit: Max results returned.
            min_score: Drop results below this threshold.
            only_alive: Only search alive agents.
            prefer_available: When True, exclude OVERLOADED agents and
                boost by capacity (same semantics as ``rank()``).
            exclude_agent_ids: Exclude specific agents (e.g. self).

        Score rules (cumulative across fields):
            - Exact match       +3.0
            - Prefix match      +1.5
            - Substring match   +0.8
            - Capacity bonus when ``prefer_available=True``
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
            if prefer_available and r.capacity_status == CapacityStatus.OVERLOADED:
                continue

            score, matched_pairs = _score_record(r, q, active_fields)
            if score < min_score:
                continue

            if prefer_available:
                if r.queue_depth == 0:
                    score += self.SCORE_QUEUE_EMPTY
                elif r.max_concurrent_tasks > 0:
                    free_slots = r.max_concurrent_tasks - r.queue_depth
                    score += max(0.0, self.SCORE_BUSY_PROPORTION * free_slots / r.max_concurrent_tasks)
            elif r.status == "idle":
                score += self.SCORE_STATUS_IDLE

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
        return 3.0
    if hay.startswith(needle):
        return 1.5
    if needle in hay:
        return 0.8
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
    """ASCII table of alive agents with capacity info."""
    records = registry.list_alive()
    if not records:
        return "(no agents online)"
    lines = [
        f"{'agent_id':25s} | {'backend':12s} | {'capacity':10s} | {'q':>3s} | {'capabilities':30s} | last_seen",
        "-" * 110,
    ]
    for r in records:
        caps = ",".join(r.capabilities[:3])
        if len(r.capabilities) > 3:
            caps += f"+{len(r.capabilities) - 3}"
        cap_status = r.capacity_status.value
        lines.append(
            f"{r.agent_id:25s} | {r.backend_id:12s} | {cap_status:10s} | {r.queue_depth:3d} | {caps:30s} | {r.last_seen[:19]}"
        )
    return "\n".join(lines)
