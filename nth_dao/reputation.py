"""
Reputation  Agent  +

 Agent  Agent



- 0.0 ~ 5.00 = 5 =
- code_review, security_audit, chat, task_execution, ...
-
- A  B  A
- team_reputation/{rater_agent_id}.json
- /


    ReputationEntry:
        rater: agent_id           #
        subject: agent_id         #
        context: str              #  /  /
        score: float              # 0.0-5.0
        reason: str               #
        timestamp: str
        sig: str                  #
        evidence: dict            #  tx hash


    rep = team.reputation

    #
    rep.rate("bob", context="code_review", score=4.5,
             reason="thorough review, caught 3 edge cases")

    #  agent
    score = rep.get_score("bob", context="code_review")
    print(f"Bob's code review score: {score['weighted_avg']:.1f}/5.0")

    #
    all = rep.get_score("bob")

    #  agent
    rep.import_entry(bob_rates_carol_entry)
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import AgentIdentity


#

DEFAULT_REPUTATION_DIR = "team_reputation"
SCORE_MIN = 0.0
SCORE_MAX = 5.0
DEFAULT_WEIGHT = 1.0


#


@dataclass
class ReputationEntry:
    """"""

    rater: str          #  agent_id
    subject: str        #  agent_id
    context: str        #  "code_review", "chat", "security"
    score: float        # 0.0-5.0
    reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""       #  Ed25519
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        #
        self.score = max(SCORE_MIN, min(SCORE_MAX, self.score))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReputationEntry":
        return cls(
            rater=data.get("rater", "?"),
            subject=data.get("subject", "?"),
            context=data.get("context", "unknown"),
            score=float(data.get("score", 2.5)),
            reason=data.get("reason", ""),
            timestamp=data.get("timestamp", ""),
            sig=data.get("sig", ""),
            evidence=data.get("evidence", {}),
        )

    def is_valid(self) -> bool:
        """ + """
        if not self.rater or not self.subject:
            return False
        if self.score < SCORE_MIN or self.score > SCORE_MAX:
            return False
        if self.rater == self.subject:
            return False  #
        return True

    def __repr__(self) -> str:
        return (
            f"[{self.context}] {self.rater[:8]}  {self.subject[:8]}: "
            f"{self.score:.1f}/5.0 ({self.reason[:30]})"
        )


@dataclass
class ReputationScore:
    """Agent  context """

    subject: str
    context: str = "*"  # "*" =
    total_entries: int = 0
    average: float = 0.0
    weighted_average: float = 0.0  #  rater
    min_score: float = 5.0
    max_score: float = 0.0
    raters: List[str] = field(default_factory=list)
    last_rated: str = ""

    def summary(self) -> str:
        if self.total_entries == 0:
            return f"{self.subject[:8]}: no ratings for [{self.context}]"
        return (
            f"{self.subject[:8]} [{self.context}]: "
            f"{self.weighted_average:.1f}/5.0 "
            f"({self.total_entries} ratings from {len(set(self.raters))} peers)"
        )


#  ReputationManager


class ReputationManager:
    """Agent """

    def __init__(
        self,
        workspace: Path,
        agent_id: str,
        identity: Optional[AgentIdentity] = None,
        reputation_dir: str = DEFAULT_REPUTATION_DIR,
    ):
        """
        Args:
            workspace:
            agent_id:  Agent ID
            identity:
            reputation_dir:
        """
        self.workspace = workspace
        self.agent_id = agent_id
        self.identity = identity
        self.base_dir = workspace / reputation_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        #  +
        self._entries: List[ReputationEntry] = []

    #

    def rate(
        self,
        subject: str,
        context: str,
        score: float,
        reason: str = "",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> ReputationEntry:
        """ Agent

        Args:
            subject:  agent_id
            context: code_review / chat / security / task / ...
            score: 0.0-5.0
            reason:
            evidence:

        Returns:
             ReputationEntry
        """
        entry = ReputationEntry(
            rater=self.agent_id,
            subject=subject,
            context=context,
            score=score,
            reason=reason,
            evidence=evidence or {},
        )

        #
        if self.identity and self.identity.can_sign:
            payload = {k: v for k, v in entry.to_dict().items() if k != "sig"}
            entry.sig = self.identity.sign_json(payload)

        self._append(entry)
        return entry

    def import_entry(self, entry: ReputationEntry) -> bool:
        """

         pubkey

        Returns:
            True
        """
        if not entry.is_valid():
            return False

        #
        for existing in self._entries:
            if (existing.rater == entry.rater
                and existing.subject == entry.subject
                and existing.context == entry.context
                and existing.timestamp == entry.timestamp):
                return False

        self._append(entry)
        return True

    #

    def get_score(
        self,
        subject: str,
        context: Optional[str] = None,
        rater_trust: Optional[Dict[str, float]] = None,
    ) -> ReputationScore:
        """ Agent

        Args:
            subject:  agent_id
            context: None =
            rater_trust:  {rater_id: trust_weight}

        Returns:
            ReputationScore
        """
        entries = [
            e for e in self._load_all()
            if e.subject == subject
            and (context is None or e.context == context)
        ]

        if not entries:
            return ReputationScore(
                subject=subject,
                context=context or "*",
            )

        scores = [e.score for e in entries]
        avg = sum(scores) / len(scores)

        #  rater
        weights = rater_trust or {}
        weighted_scores = [
            s * weights.get(e.rater, DEFAULT_WEIGHT)
            for s, e in zip(scores, entries)
        ]
        weighted_avg = (
            sum(weighted_scores) / sum(weights.get(e.rater, DEFAULT_WEIGHT) for e in entries)
            if weighted_scores else avg
        )

        return ReputationScore(
            subject=subject,
            context=context or "*",
            total_entries=len(entries),
            average=round(avg, 2),
            weighted_average=round(weighted_avg, 2),
            min_score=min(scores),
            max_score=max(scores),
            raters=[e.rater for e in entries],
            last_rated=max(e.timestamp for e in entries),
        )

    def get_all_scores(
        self,
        subject: Optional[str] = None,
    ) -> Dict[str, Dict[str, ReputationScore]]:
        """ agent  context

        Returns:
            {subject_id: {context: ReputationScore}}
        """
        entries = self._load_all()
        if subject:
            entries = [e for e in entries if e.subject == subject]

        result: Dict[str, Dict[str, ReputationScore]] = defaultdict(dict)
        subjects = set(e.subject for e in entries)
        contexts = set(e.context for e in entries)

        for subj in subjects:
            for ctx in contexts:
                score = self.get_score(subj, context=ctx)
                if score.total_entries > 0:
                    result[subj][ctx] = score

        return dict(result)

    def top_agents(
        self,
        context: Optional[str] = None,
        limit: int = 10,
    ) -> List[ReputationScore]:
        """

        Args:
            context:
            limit:  N
        """
        all_scores = self.get_all_scores()
        results = []

        for subject, ctx_scores in all_scores.items():
            if context and context in ctx_scores:
                results.append(ctx_scores[context])
            elif not context:
                #
                global_score = self.get_score(subject)
                if global_score.total_entries > 0:
                    results.append(global_score)

        results.sort(key=lambda s: s.weighted_average, reverse=True)
        return results[:limit]

    #

    def trust_graph(
        self,
        max_depth: int = 2,
    ) -> Dict[str, Any]:
        """

        Returns:
            {
                "nodes": {agent_id: {global_score, rating_count}},
                "edges": [(rater, subject, score, context), ...]
            }
        """
        entries = self._load_all()
        nodes: Dict[str, dict] = {}
        edges: list = []

        for e in entries:
            for agent in [e.rater, e.subject]:
                if agent not in nodes:
                    score = self.get_score(agent)
                    nodes[agent] = {
                        "global_score": score.weighted_average,
                        "rating_count": score.total_entries,
                    }
            edges.append((e.rater, e.subject, e.score, e.context))

        return {"nodes": nodes, "edges": edges}

    def compute_trust_weights(self) -> Dict[str, float]:
        """

        rater

        Returns:
            {rater_id: weight (0.0-1.0)}
        """
        entries = self._load_all()
        raters = set(e.rater for e in entries)
        weights = {}

        for rater in raters:
            score = self.get_score(rater)
            if score.total_entries >= 3:
                #
                weights[rater] = score.weighted_average / SCORE_MAX
            else:
                #
                weights[rater] = 0.5

        return weights

    #   /

    def export_entries(
        self,
        subject: Optional[str] = None,
        context: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[dict]:
        """"""
        entries = self._load_all()

        if subject:
            entries = [e for e in entries if e.subject == subject]
        if context:
            entries = [e for e in entries if e.context == context]
        if since:
            entries = [e for e in entries if e.timestamp > since]

        return [e.to_dict() for e in entries]

    def import_batch(self, entries_data: List[dict]) -> int:
        """"""
        imported = 0
        for data in entries_data:
            try:
                entry = ReputationEntry.from_dict(data)
                if self.import_entry(entry):
                    imported += 1
            except Exception:
                continue
        return imported

    #

    def stats(self) -> Dict[str, Any]:
        """"""
        entries = self._load_all()
        subjects = set(e.subject for e in entries)
        contexts = set(e.context for e in entries)

        return {
            "total_ratings": len(entries),
            "unique_subjects": len(subjects),
            "unique_contexts": len(contexts),
            "contexts": sorted(contexts),
            "top_rated": [
                {
                    "agent": s,
                    "score": self.get_score(s).weighted_average,
                    "count": self.get_score(s).total_entries,
                }
                for s in sorted(subjects, key=lambda s: self.get_score(s).weighted_average, reverse=True)[:5]
            ],
        }

    #

    def _my_file(self) -> Path:
        """ agent """
        safe_id = "".join(c if c.isalnum() or c in "_-" else "-" for c in self.agent_id)
        return self.base_dir / f"{safe_id}.json"

    def _load_all(self) -> List[ReputationEntry]:
        """ + """
        entries = []

        for f in sorted(self.base_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for item in data:
                        entries.append(ReputationEntry.from_dict(item))
                elif isinstance(data, dict):
                    entries.append(ReputationEntry.from_dict(data))
            except Exception:
                continue

        return entries

    def _append(self, entry: ReputationEntry) -> None:
        """ agent """
        file_path = self._my_file()

        #
        existing = []
        if file_path.exists():
            try:
                existing = json.loads(file_path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        existing.append(entry.to_dict())

        #
        tmp = file_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(file_path))
