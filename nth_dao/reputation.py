"""
Reputation — Agent 声誉系统（主观评分 + 信任网络）

每个 Agent 可以给其他 Agent 打分，按任务上下文分类。
声誉是主观的（本地视角），不追求全局共识——适合去中心化场景。

设计：
- 评分范围：0.0 ~ 5.0（0 = 完全不可信，5 = 完美信任）
- 按上下文分类：code_review, security_audit, chat, task_execution, ...
- 每次评分附带理由和签名（可验证）
- 信任网络：A 给 B 的评分，权重取决于 A 自身的声誉
- 持久化：team_reputation/{rater_agent_id}.json
- 可导出/导入（跨节点同步声誉数据）

数据模型：
    ReputationEntry:
        rater: agent_id           # 评分者
        subject: agent_id         # 被评分者
        context: str              # 上下文（代码审查 / 聊天 / 安全审计）
        score: float              # 0.0-5.0
        reason: str               # 理由
        timestamp: str
        sig: str                  # 签名
        evidence: dict            # 可选证据（如 tx hash）

用法：
    rep = team.reputation

    # 评分
    rep.rate("bob", context="code_review", score=4.5,
             reason="thorough review, caught 3 edge cases")

    # 查询某 agent 的综合声誉
    score = rep.get_score("bob", context="code_review")
    print(f"Bob's code review score: {score['weighted_avg']:.1f}/5.0")

    # 查询全局声誉
    all = rep.get_score("bob")

    # 导入他人对某 agent 的评分（信任网络）
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


# ─────────────────── 常量 ───────────────────

DEFAULT_REPUTATION_DIR = "team_reputation"
SCORE_MIN = 0.0
SCORE_MAX = 5.0
DEFAULT_WEIGHT = 1.0


# ─────────────────── 数据模型 ───────────────────


@dataclass
class ReputationEntry:
    """一条声誉评分记录"""

    rater: str          # 评分者 agent_id
    subject: str        # 被评分者 agent_id
    context: str        # 上下文（如 "code_review", "chat", "security"）
    score: float        # 0.0-5.0
    reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""       # 评分者的 Ed25519 签名
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 强制分数在范围内
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
        """基本合法性检查：分数范围 + 必要字段"""
        if not self.rater or not self.subject:
            return False
        if self.score < SCORE_MIN or self.score > SCORE_MAX:
            return False
        if self.rater == self.subject:
            return False  # 不能给自评分
        return True

    def __repr__(self) -> str:
        return (
            f"[{self.context}] {self.rater[:8]} → {self.subject[:8]}: "
            f"{self.score:.1f}/5.0 ({self.reason[:30]})"
        )


@dataclass
class ReputationScore:
    """Agent 的声誉统计（单个 context 或全局）"""

    subject: str
    context: str = "*"  # "*" = 全局
    total_entries: int = 0
    average: float = 0.0
    weighted_average: float = 0.0  # 考虑 rater 声誉
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


# ─────────────────── ReputationManager ───────────────────


class ReputationManager:
    """Agent 声誉管理器"""

    def __init__(
        self,
        workspace: Path,
        agent_id: str,
        identity: Optional[AgentIdentity] = None,
        reputation_dir: str = DEFAULT_REPUTATION_DIR,
    ):
        """
        Args:
            workspace: 团队工作目录
            agent_id: 本 Agent ID
            identity: 密码学身份（签名评分用）
            reputation_dir: 声誉数据子目录名
        """
        self.workspace = workspace
        self.agent_id = agent_id
        self.identity = identity
        self.base_dir = workspace / reputation_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓存（本地评分 + 导入的）
        self._entries: List[ReputationEntry] = []

    # ─────────── 评分 ───────────

    def rate(
        self,
        subject: str,
        context: str,
        score: float,
        reason: str = "",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> ReputationEntry:
        """给指定 Agent 评分

        Args:
            subject: 被评分的 agent_id
            context: 上下文（code_review / chat / security / task / ...）
            score: 0.0-5.0
            reason: 评分理由
            evidence: 可选证据

        Returns:
            创建的 ReputationEntry
        """
        entry = ReputationEntry(
            rater=self.agent_id,
            subject=subject,
            context=context,
            score=score,
            reason=reason,
            evidence=evidence or {},
        )

        # 签名
        if self.identity and self.identity.can_sign:
            payload = {k: v for k, v in entry.to_dict().items() if k != "sig"}
            entry.sig = self.identity.sign_json(payload)

        self._append(entry)
        return entry

    def import_entry(self, entry: ReputationEntry) -> bool:
        """导入他人创建的评分（信任网络）

        验证签名（如有 pubkey）后存入本地。

        Returns:
            True 如果导入成功
        """
        if not entry.is_valid():
            return False

        # 去重
        for existing in self._entries:
            if (existing.rater == entry.rater
                and existing.subject == entry.subject
                and existing.context == entry.context
                and existing.timestamp == entry.timestamp):
                return False

        self._append(entry)
        return True

    # ─────────── 查询 ───────────

    def get_score(
        self,
        subject: str,
        context: Optional[str] = None,
        rater_trust: Optional[Dict[str, float]] = None,
    ) -> ReputationScore:
        """查询某个 Agent 的声誉

        Args:
            subject: 被查询的 agent_id
            context: 上下文（None = 所有上下文）
            rater_trust: 评分者的权重表 {rater_id: trust_weight}

        Returns:
            ReputationScore 统计
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

        # 加权平均（考虑 rater 声誉）
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
        """查询所有 agent 的声誉（按 context 分组）

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
        """按声誉排名

        Args:
            context: 按上下文筛选
            limit: 返回前 N 名
        """
        all_scores = self.get_all_scores()
        results = []

        for subject, ctx_scores in all_scores.items():
            if context and context in ctx_scores:
                results.append(ctx_scores[context])
            elif not context:
                # 全局平均
                global_score = self.get_score(subject)
                if global_score.total_entries > 0:
                    results.append(global_score)

        results.sort(key=lambda s: s.weighted_average, reverse=True)
        return results[:limit]

    # ─────────── 信任网络 ───────────

    def trust_graph(
        self,
        max_depth: int = 2,
    ) -> Dict[str, Any]:
        """构建信任网络图（用于计算传递信任权重）

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
        """计算评分者信任权重（基于他们的全局声誉）

        rater 的全局声誉越高，他的评分权重越大。

        Returns:
            {rater_id: weight (0.0-1.0)}
        """
        entries = self._load_all()
        raters = set(e.rater for e in entries)
        weights = {}

        for rater in raters:
            score = self.get_score(rater)
            if score.total_entries >= 3:
                # 有足够评分 → 权重基于声誉
                weights[rater] = score.weighted_average / SCORE_MAX
            else:
                # 评分不足 → 默认中等权重
                weights[rater] = 0.5

        return weights

    # ─────────── 导出 / 导入 ───────────

    def export_entries(
        self,
        subject: Optional[str] = None,
        context: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[dict]:
        """导出评分数据（用于跨节点同步）"""
        entries = self._load_all()

        if subject:
            entries = [e for e in entries if e.subject == subject]
        if context:
            entries = [e for e in entries if e.context == context]
        if since:
            entries = [e for e in entries if e.timestamp > since]

        return [e.to_dict() for e in entries]

    def import_batch(self, entries_data: List[dict]) -> int:
        """批量导入评分（跨节点同步）"""
        imported = 0
        for data in entries_data:
            try:
                entry = ReputationEntry.from_dict(data)
                if self.import_entry(entry):
                    imported += 1
            except Exception:
                continue
        return imported

    # ─────────── 统计 ───────────

    def stats(self) -> Dict[str, Any]:
        """声誉统计总览"""
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

    # ─────────── 内部 ───────────

    def _my_file(self) -> Path:
        """本 agent 的声誉文件路径"""
        safe_id = "".join(c if c.isalnum() or c in "_-" else "-" for c in self.agent_id)
        return self.base_dir / f"{safe_id}.json"

    def _load_all(self) -> List[ReputationEntry]:
        """加载所有已知的声誉数据（本地 + 导入的）"""
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
        """追加一条评分到本 agent 的声誉文件"""
        file_path = self._my_file()

        # 读取已有数据
        existing = []
        if file_path.exists():
            try:
                existing = json.loads(file_path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        existing.append(entry.to_dict())

        # 原子写入
        tmp = file_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(file_path))
