"""
Marketplace — Agent 服务交易市场（任务发布 / 认领 / 结算）

一个去中心化的 Agent 任务市场，允许 Agent 发布任务、竞争性认领、完成后获得
声誉积分（credits）。适合 Agent-to-Agent 协作和自动化工作流。

设计：
- 任务状态机：open → claimed → in_progress → completed/failed/cancelled
- 声誉门槛：认领者必须满足最低声誉分数
- 积分系统（credits）：内部信用，未来可对接链上结算
- 托管机制：认领时锁定奖励，完成时释放
- 争议处理：逾期自动释放，争议进入 review
- 持久化：team_marketplace/orders/{order_id}.json
- 审计：所有状态变更记录在 order timeline，带签名

生命周期：
    发布者 → create_order("review PR #42", reward=10)
    认领者 → claim(order_id)          # 检查声誉门槛
    认领者 → submit(order_id, proof)  # 提交成果
    发布者 → accept(order_id)          # 验收通过，释放积分
    发布者 → reject(order_id, reason)  # 拒绝，退还任务

用法：
    mkt = team.marketplace

    # 发布任务
    order = mkt.create_order(
        title="review PR #42",
        description="need thorough code review of the auth module",
        context="code_review",
        reward=10,
        min_reputation=3.0,
    )

    # 浏览任务
    available = mkt.list_open()

    # 认领
    mkt.claim(available[0].order_id)

    # 提交
    mkt.submit(order_id, proof="review completed, 3 issues found")

    # 验收
    mkt.accept(order_id, rating=4.5, note="great work!")
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import AgentIdentity
from .channel import TeamChannel
from .reputation import ReputationManager


# ─────────────────── 常量 ───────────────────

DEFAULT_MARKETPLACE_DIR = "team_marketplace"
DEFAULT_CLAIM_TIMEOUT_HOURS = 48  # 认领后必须在此时间内提交
DEFAULT_ORDER_EXPIRY_DAYS = 7     # 公开任务过期天数


# ─────────────────── 枚举 ───────────────────


class OrderStatus(str, Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    EXPIRED = "expired"


# ─────────────────── 数据模型 ───────────────────


@dataclass
class TaskOrder:
    """一个任务订单"""

    order_id: str
    creator: str          # 发布者 agent_id
    title: str
    description: str = ""
    context: str = "general"  # code_review / bug_fix / research / write_docs / deploy / custom
    reward: float = 0.0   # 信用积分
    deadline: str = ""    # ISO timestamp，空 = 无期限
    tags: List[str] = field(default_factory=list)
    requirements: Dict[str, Any] = field(default_factory=dict)  # min_reputation, capabilities等

    # 状态追踪
    status: OrderStatus = OrderStatus.OPEN
    claimant: str = ""    # 认领者 agent_id
    submission_proof: str = ""  # 提交的成果说明
    submission_at: str = ""
    rating: float = 0.0   # 完成后发布者对认领者的评分
    feedback: str = ""     # 发布者反馈

    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    claimed_at: str = ""
    completed_at: str = ""

    # 审计追踪（timeline）
    timeline: List[Dict[str, Any]] = field(default_factory=list)

    # 签名
    creator_sig: str = ""
    claimant_sig: str = ""

    def __post_init__(self):
        if not self.timeline:
            self.timeline = [{
                "action": "created",
                "actor": self.creator,
                "timestamp": self.created_at,
            }]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TaskOrder":
        status_raw = data.get("status", "open")
        try:
            status = OrderStatus(status_raw)
        except ValueError:
            status = OrderStatus.OPEN

        return cls(
            order_id=data.get("order_id", ""),
            creator=data.get("creator", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            context=data.get("context", "general"),
            reward=float(data.get("reward", 0)),
            deadline=data.get("deadline", ""),
            tags=data.get("tags", []),
            requirements=data.get("requirements", {}),
            status=status,
            claimant=data.get("claimant", ""),
            submission_proof=data.get("submission_proof", ""),
            submission_at=data.get("submission_at", ""),
            rating=float(data.get("rating", 0)),
            feedback=data.get("feedback", ""),
            created_at=data.get("created_at", ""),
            claimed_at=data.get("claimed_at", ""),
            completed_at=data.get("completed_at", ""),
            timeline=data.get("timeline", []),
            creator_sig=data.get("creator_sig", ""),
            claimant_sig=data.get("claimant_sig", ""),
        )

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.OPEN,
            OrderStatus.CLAIMED,
            OrderStatus.IN_PROGRESS,
            OrderStatus.SUBMITTED,
            OrderStatus.DISPUTED,
        )

    @property
    def is_finished(self) -> bool:
        return self.status in (
            OrderStatus.COMPLETED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        )

    @property
    def short_id(self) -> str:
        return self.order_id[:8]

    def __repr__(self) -> str:
        return (
            f"[{self.status.value}] {self.short_id}: {self.title[:40]} "
            f"(reward={self.reward}, creator={self.creator[:8]})"
        )


# ─────────────────── TaskMarketplace ───────────────────


class TaskMarketplace:
    """Agent 服务交易市场"""

    def __init__(
        self,
        workspace: Path,
        agent_id: str,
        identity: Optional[AgentIdentity] = None,
        channel: Optional[TeamChannel] = None,
        reputation: Optional[ReputationManager] = None,
        marketplace_dir: str = DEFAULT_MARKETPLACE_DIR,
    ):
        """
        Args:
            workspace: 团队工作目录
            agent_id: 本 Agent ID
            identity: 密码学身份
            channel: 消息通道（用于通知）
            reputation: 声誉管理器（用于准入门槛）
            marketplace_dir: 订单存储子目录
        """
        self.workspace = workspace
        self.agent_id = agent_id
        self.identity = identity
        self.channel = channel
        self.reputation = reputation
        self.orders_dir = workspace / marketplace_dir
        self.orders_dir.mkdir(parents=True, exist_ok=True)

        # 本地积分余额
        self._credit_file = workspace / marketplace_dir / f"{self._safe_id(agent_id)}_credits.json"
        self._init_credits()

    # ─────────── 积分 ───────────

    def _init_credits(self) -> None:
        if not self._credit_file.exists():
            self._write_credits(100)  # 新 agent 初始积分

    def _read_credits(self) -> float:
        try:
            return json.loads(self._credit_file.read_text()).get("balance", 0)
        except Exception:
            return 0

    def _write_credits(self, balance: float) -> None:
        tmp = self._credit_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"agent_id": self.agent_id, "balance": round(balance, 2)}, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(self._credit_file))

    @property
    def balance(self) -> float:
        return self._read_credits()

    # ─────────── 创建订单 ───────────

    def create_order(
        self,
        title: str,
        description: str = "",
        context: str = "general",
        reward: float = 0.0,
        deadline: Optional[str] = None,
        tags: Optional[List[str]] = None,
        min_reputation: float = 0.0,
        required_capabilities: Optional[List[str]] = None,
    ) -> TaskOrder:
        """发布一个新任务

        Args:
            title: 任务标题
            description: 详细描述
            context: 任务类型
            reward: 积分奖励
            deadline: ISO 截止日期
            tags: 标签
            min_reputation: 认领者的最低声誉分数
            required_capabilities: 认领者需具备的能力

        Returns:
            创建的订单

        Raises:
            ValueError: 积分不足
        """
        # 检查余额
        if reward > 0 and self._read_credits() < reward:
            raise ValueError(
                f"Insufficient credits: balance={self._read_credits()}, "
                f"reward={reward}"
            )

        import uuid
        order = TaskOrder(
            order_id=uuid.uuid4().hex,
            creator=self.agent_id,
            title=title,
            description=description,
            context=context,
            reward=reward,
            deadline=deadline or "",
            tags=tags or [],
            requirements={
                "min_reputation": min_reputation,
                "capabilities": required_capabilities or [],
            },
        )

        # 签名
        if self.identity and self.identity.can_sign:
            payload = {
                "order_id": order.order_id,
                "creator": order.creator,
                "title": order.title,
                "reward": order.reward,
                "created_at": order.created_at,
            }
            order.creator_sig = self.identity.sign_json(payload)

        # 锁定积分（托管）
        if reward > 0:
            self._write_credits(self._read_credits() - reward)

        self._save(order)

        # 通知团队
        if self.channel:
            self.channel.send(
                f"📋 New task: {title} (reward={reward} credits, context={context})",
                scope="team",
                metadata={"type": "marketplace", "order_id": order.order_id},
            )

        return order

    # ─────────── 认领 ───────────

    def claim(self, order_id: str) -> TaskOrder:
        """认领一个公开任务

        Args:
            order_id: 订单 ID

        Raises:
            ValueError: 订单不可认领
            PermissionError: 不满足声誉门槛
        """
        order = self._load(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")

        if order.status != OrderStatus.OPEN:
            raise ValueError(f"Order {order_id[:8]} is {order.status.value}, not open")

        if order.creator == self.agent_id:
            raise ValueError("Cannot claim your own order")

        # 检查声誉门槛
        min_rep = order.requirements.get("min_reputation", 0)
        if min_rep > 0 and self.reputation:
            score = self.reputation.get_score(self.agent_id, context=order.context)
            if score.weighted_average < min_rep:
                raise PermissionError(
                    f"Reputation too low: {score.weighted_average:.1f} < {min_rep} "
                    f"for context [{order.context}]"
                )

        now = datetime.now().isoformat()
        order.status = OrderStatus.CLAIMED
        order.claimant = self.agent_id
        order.claimed_at = now
        order.timeline.append({
            "action": "claimed",
            "actor": self.agent_id,
            "timestamp": now,
        })

        # 签名
        if self.identity and self.identity.can_sign:
            payload = {
                "order_id": order.order_id,
                "claimant": self.agent_id,
                "claimed_at": now,
            }
            order.claimant_sig = self.identity.sign_json(payload)

        self._save(order)

        # 通知创建者
        if self.channel:
            self.channel.dm(
                order.creator,
                f"🤝 Your task '{order.title}' was claimed by "
                f"{self.agent_id[:8]}",
            )

        return order

    # ─────────── 执行 ───────────

    def start_work(self, order_id: str) -> TaskOrder:
        """确认开始执行"""
        order = self._require_my_claim(order_id)
        order.status = OrderStatus.IN_PROGRESS
        order.timeline.append({
            "action": "started",
            "actor": self.agent_id,
            "timestamp": datetime.now().isoformat(),
        })
        self._save(order)
        return order

    def submit(self, order_id: str, proof: str) -> TaskOrder:
        """提交完成成果

        Args:
            order_id: 订单 ID
            proof: 成果描述 / 证明

        Raises:
            ValueError: 订单状态不对
        """
        order = self._require_my_claim(order_id)
        if order.status not in (OrderStatus.CLAIMED, OrderStatus.IN_PROGRESS):
            raise ValueError(
                f"Order {order_id[:8]} is {order.status.value}, "
                f"cannot submit"
            )

        now = datetime.now().isoformat()
        order.status = OrderStatus.SUBMITTED
        order.submission_proof = proof
        order.submission_at = now
        order.timeline.append({
            "action": "submitted",
            "actor": self.agent_id,
            "timestamp": now,
            "proof": proof[:200],
        })

        self._save(order)

        # 通知创建者
        if self.channel:
            self.channel.dm(
                order.creator,
                f"✅ Task '{order.title}' submitted by {self.agent_id[:8]}: "
                f"{proof[:100]}",
            )

        return order

    # ─────────── 验收 ───────────

    def accept(
        self,
        order_id: str,
        rating: float = 5.0,
        feedback: str = "",
    ) -> TaskOrder:
        """验收任务，释放积分给认领者

        Args:
            order_id: 订单 ID
            rating: 对认领者的评分 (0.0-5.0)
            feedback: 反馈
        """
        order = self._require_my_order(order_id)
        if order.status != OrderStatus.SUBMITTED:
            raise ValueError(
                f"Order {order_id[:8]} is {order.status.value}, not submitted"
            )

        now = datetime.now().isoformat()
        order.status = OrderStatus.COMPLETED
        order.rating = max(0, min(5, rating))
        order.feedback = feedback
        order.completed_at = now
        order.timeline.append({
            "action": "completed",
            "actor": self.agent_id,
            "timestamp": now,
            "rating": order.rating,
        })

        self._save(order)

        # 声誉评分（给认领者）
        if self.reputation:
            self.reputation.rate(
                subject=order.claimant,
                context=order.context,
                score=order.rating,
                reason=feedback or f"Completed: {order.title}",
            )

        # 通知认领者
        if self.channel:
            self.channel.dm(
                order.claimant,
                f"🎉 Your task '{order.title}' was accepted! "
                f"Rating: {order.rating}/5.0. You earned {order.reward} credits.",
            )

        return order

    def reject(
        self,
        order_id: str,
        reason: str = "",
    ) -> TaskOrder:
        """拒绝提交，任务重新变为 open

        Args:
            order_id: 订单 ID
            reason: 拒绝理由
        """
        order = self._require_my_order(order_id)
        if order.status != OrderStatus.SUBMITTED:
            raise ValueError(
                f"Order {order_id[:8]} is {order.status.value}, not submitted"
            )

        now = datetime.now().isoformat()
        order.status = OrderStatus.OPEN
        order.claimant = ""
        order.claimant_sig = ""
        order.submission_proof = ""
        order.submission_at = ""
        order.timeline.append({
            "action": "rejected",
            "actor": self.agent_id,
            "timestamp": now,
            "reason": reason,
        })

        self._save(order)

        if self.channel:
            self.channel.dm(
                order.claimant if hasattr(order, 'claimant_history') else "unknown",
                f"❌ Your submission for '{order.title}' was rejected: {reason}",
            )

        return order

    def cancel(self, order_id: str) -> TaskOrder:
        """发布者取消任务（仅限未被认领的）"""
        order = self._require_my_order(order_id)
        if order.status not in (OrderStatus.OPEN, OrderStatus.EXPIRED):
            raise ValueError(
                f"Cannot cancel order in {order.status.value} state"
            )

        order.status = OrderStatus.CANCELLED
        order.timeline.append({
            "action": "cancelled",
            "actor": self.agent_id,
            "timestamp": datetime.now().isoformat(),
        })

        # 退还积分
        if order.reward > 0:
            self._write_credits(self._read_credits() + order.reward)

        self._save(order)
        return order

    def fail(self, order_id: str, reason: str = "") -> TaskOrder:
        """认领者放弃任务"""
        order = self._require_my_claim(order_id)
        if order.status not in (OrderStatus.CLAIMED, OrderStatus.IN_PROGRESS):
            raise ValueError(
                f"Order {order_id[:8]} is {order.status.value}, cannot fail"
            )

        order.status = OrderStatus.FAILED
        order.timeline.append({
            "action": "failed",
            "actor": self.agent_id,
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
        })

        self._save(order)
        return order

    # ─────────── 查询 ───────────

    def list_open(self, context: Optional[str] = None) -> List[TaskOrder]:
        """列出所有公开任务"""
        orders = []
        for f in self.orders_dir.glob("*.json"):
            try:
                o = TaskOrder.from_dict(json.loads(f.read_text(encoding="utf-8")))
                if o.status == OrderStatus.OPEN:
                    if context is None or o.context == context:
                        orders.append(o)
            except Exception:
                continue

        # 按创建时间排序（最新优先）
        orders.sort(key=lambda o: o.created_at, reverse=True)
        return orders

    def list_my_orders(
        self,
        status: Optional[OrderStatus] = None,
    ) -> List[TaskOrder]:
        """列出我的订单（我创建或认领的）"""
        orders = []
        for f in self.orders_dir.glob("*.json"):
            try:
                o = TaskOrder.from_dict(json.loads(f.read_text(encoding="utf-8")))
                if o.creator == self.agent_id or o.claimant == self.agent_id:
                    if status is None or o.status == status:
                        orders.append(o)
            except Exception:
                continue

        orders.sort(key=lambda o: o.created_at, reverse=True)
        return orders

    def list_by_context(self, context: str) -> List[TaskOrder]:
        """按任务类型列出"""
        return self.list_open(context=context)

    def get_order(self, order_id: str) -> Optional[TaskOrder]:
        return self._load(order_id)

    # ─────────── 统计 ───────────

    def stats(self) -> Dict[str, Any]:
        stats = {
            "total": 0,
            "open": 0,
            "claimed": 0,
            "in_progress": 0,
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "total_reward": 0.0,
        }

        for f in self.orders_dir.glob("*.json"):
            try:
                o = TaskOrder.from_dict(json.loads(f.read_text(encoding="utf-8")))
                stats["total"] += 1
                stats[o.status.value] = stats.get(o.status.value, 0) + 1
                if o.status == OrderStatus.COMPLETED:
                    stats["total_reward"] += o.reward
            except Exception:
                continue

        stats["balance"] = self.balance
        return stats

    # ─────────── 过期检查 ───────────

    def check_expired(self) -> int:
        """检查并标记过期任务"""
        now = datetime.now()
        expired_count = 0

        for f in self.orders_dir.glob("*.json"):
            try:
                o = TaskOrder.from_dict(json.loads(f.read_text(encoding="utf-8")))
                if not o.is_active:
                    continue

                created = datetime.fromisoformat(o.created_at)

                # 公开任务过期
                if o.status == OrderStatus.OPEN:
                    if (now - created).days >= DEFAULT_ORDER_EXPIRY_DAYS:
                        o.status = OrderStatus.EXPIRED
                        o.timeline.append({
                            "action": "expired",
                            "actor": "system",
                            "timestamp": now.isoformat(),
                        })
                        self._save(o)
                        # 退还积分
                        if o.reward > 0:
                            self._write_credits(self._read_credits() + o.reward)
                        expired_count += 1

                # 认领后超时
                if o.status in (OrderStatus.CLAIMED, OrderStatus.IN_PROGRESS) and o.claimed_at:
                    claimed = datetime.fromisoformat(o.claimed_at)
                    if (now - claimed).total_seconds() > DEFAULT_CLAIM_TIMEOUT_HOURS * 3600:
                        o.status = OrderStatus.OPEN
                        o.claimant = ""
                        o.claimant_sig = ""
                        o.timeline.append({
                            "action": "claim_expired",
                            "actor": "system",
                            "timestamp": now.isoformat(),
                        })
                        self._save(o)
                        expired_count += 1

            except Exception:
                continue

        return expired_count

    # ─────────── 内部 ───────────

    def _require_my_order(self, order_id: str) -> TaskOrder:
        order = self._load(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")
        if order.creator != self.agent_id:
            raise PermissionError(
                f"Order {order_id[:8]} belongs to {order.creator[:8]}, not you"
            )
        return order

    def _require_my_claim(self, order_id: str) -> TaskOrder:
        order = self._load(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")
        if order.claimant != self.agent_id:
            raise PermissionError(
                f"Order {order_id[:8]} is claimed by "
                f"{order.claimant[:8] if order.claimant else 'no one'}, not you"
            )
        return order

    def _load(self, order_id: str) -> Optional[TaskOrder]:
        path = self.orders_dir / f"{order_id}.json"
        if not path.exists():
            return None
        try:
            return TaskOrder.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def _save(self, order: TaskOrder) -> None:
        path = self.orders_dir / f"{order.order_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(order.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))

    @staticmethod
    def _safe_id(agent_id: str) -> str:
        return "".join(c if c.isalnum() or c in "_-" else "-" for c in agent_id)
