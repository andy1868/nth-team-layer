"""
Membership — Agent 申请/审批加入团队机制

类似微信群、QQ 群：agent 需要申请加入，
现有成员可以 approve 或 reject，支持多种 join_policy。

设计：
  1. team.json — 团队配置（包含 join_policy、管理员、现有成员）
  2. team_agents/requests/{agent_id}.json — 待审批的申请
  3. 审批通过后 → 申请移到 team_agents/{agent_id}.json（正式成员）
  4. 支持 join_policy:
     - "open"       — 无需审批，直接加入（默认，向后兼容）
     - "approval"   — 需要现有成员 approve
     - "invite_only" — 只能由管理员邀请加入
     - "token"      — 需要匹配 join_token（类似微信群二维码/邀请码）

用法示例：
  # 创建团队（管理员）
  team = nth.attach(agent_id="admin", ...)
  team.membership.init_team(policy="approval", admin_ids=["admin"])
  
  # 申请加入
  team.membership.request_join(agent_id="newbie", capabilities=["python"], ...)
  
  # 管理员审批
  admin_team.membership.approve("newbie")
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─────────────────── 常量 ───────────────────

DEFAULT_TEAM_CONFIG = "team.json"
DEFAULT_REQUESTS_DIR = "team_agents/requests"


# ─────────────────── 枚举 ───────────────────


class JoinPolicy(str, Enum):
    OPEN = "open"             # 自由加入
    APPROVAL = "approval"     # 需审批
    INVITE_ONLY = "invite_only"  # 仅管理员邀请
    TOKEN = "token"           # 邀请码/令牌


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ─────────────────── 数据类 ───────────────────


@dataclass
class TeamConfig:
    """团队全局配置"""
    team_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    team_name: str = "Unnamed Team"
    join_policy: JoinPolicy = JoinPolicy.OPEN
    join_token: str = ""  # 仅 token 模式使用
    admin_ids: List[str] = field(default_factory=list)
    member_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["join_policy"] = self.join_policy.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TeamConfig":
        policy_raw = data.get("join_policy", "open")
        try:
            policy = JoinPolicy(policy_raw)
        except ValueError:
            policy = JoinPolicy.OPEN
        return cls(
            team_id=data.get("team_id", uuid.uuid4().hex[:8]),
            team_name=data.get("team_name", "Unnamed Team"),
            join_policy=policy,
            join_token=data.get("join_token", ""),
            admin_ids=data.get("admin_ids", []),
            member_ids=data.get("member_ids", []),
            created_at=data.get("created_at", datetime.now().isoformat()),
            metadata=data.get("metadata", {}),
        )

    def is_admin(self, agent_id: str) -> bool:
        return agent_id in self.admin_ids

    def is_member(self, agent_id: str) -> bool:
        return agent_id in self.member_ids


@dataclass
class JoinRequest:
    """Agent 加入申请"""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    agent_id: str = ""
    capabilities: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    backend_id: str = "unknown"
    hostname: str = ""
    pid: int = 0
    message: str = ""           # 申请理由（类似 QQ 群验证消息）
    status: RequestStatus = RequestStatus.PENDING
    reviewed_by: str = ""        # 审批人 agent_id
    review_note: str = ""        # 审批备注
    submitted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    reviewed_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "JoinRequest":
        status_raw = data.get("status", "pending")
        try:
            status = RequestStatus(status_raw)
        except ValueError:
            status = RequestStatus.PENDING
        return cls(
            request_id=data.get("request_id", uuid.uuid4().hex[:8]),
            agent_id=data.get("agent_id", ""),
            capabilities=data.get("capabilities", []),
            groups=data.get("groups", []),
            backend_id=data.get("backend_id", "unknown"),
            hostname=data.get("hostname", ""),
            pid=data.get("pid", 0),
            message=data.get("message", ""),
            status=status,
            reviewed_by=data.get("reviewed_by", ""),
            review_note=data.get("review_note", ""),
            submitted_at=data.get("submitted_at", ""),
            reviewed_at=data.get("reviewed_at", ""),
            metadata=data.get("metadata", {}),
        )


# ─────────────────── MembershipManager ───────────────────


class MembershipManager:
    """团队会员管理 — 申请 / 审批 / 踢出 / 邀请"""

    MAX_REQUEST_AGE_SECONDS = 86400 * 7  # 申请 7 天过期

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ─────────── Team Config ───────────

    @property
    def config_path(self) -> Path:
        return self.workspace / DEFAULT_TEAM_CONFIG

    def load_config(self) -> TeamConfig:
        """加载团队配置，不存在则返回默认（open 模式）"""
        if not self.config_path.exists():
            return TeamConfig()
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return TeamConfig.from_dict(data)
        except Exception:
            return TeamConfig()

    def save_config(self, config: TeamConfig) -> None:
        """保存团队配置（原子写入）"""
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(self.config_path))

    def init_team(
        self,
        team_name: str = "My Team",
        policy: JoinPolicy = JoinPolicy.OPEN,
        join_token: str = "",
        admin_ids: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TeamConfig:
        """
        初始化/更新团队配置。
        创建者自动成为管理员 + 首个成员。
        """
        config = self.load_config()
        config.team_name = team_name
        config.join_policy = policy
        config.join_token = join_token

        admin_ids = admin_ids or []
        if admin_ids:
            config.admin_ids = list(set(config.admin_ids + admin_ids))
        # 如果没有管理员，默认不自动添加（由调用方决定）

        if metadata:
            config.metadata.update(metadata)

        self.save_config(config)
        return config

    def set_policy(self, policy: JoinPolicy, join_token: str = "") -> TeamConfig:
        """快速修改团队的加入策略"""
        config = self.load_config()
        config.join_policy = policy
        if join_token:
            config.join_token = join_token
        self.save_config(config)
        return config

    def add_admin(self, agent_id: str) -> TeamConfig:
        config = self.load_config()
        if agent_id not in config.admin_ids:
            config.admin_ids.append(agent_id)
        self.save_config(config)
        return config

    def remove_admin(self, agent_id: str) -> TeamConfig:
        config = self.load_config()
        if agent_id in config.admin_ids:
            config.admin_ids.remove(agent_id)
        self.save_config(config)
        return config

    # ─────────── 加入检查 ───────────

    def can_join(self, agent_id: str, token: str = "") -> tuple[bool, str]:
        """
        检查 agent 是否可以加入。
        
        Returns:
            (allowed, reason)
        """
        config = self.load_config()

        # 已是成员
        if agent_id in config.member_ids:
            return True, "already_member"

        policy = config.join_policy

        if policy == JoinPolicy.OPEN:
            return True, "open_policy"

        if policy == JoinPolicy.APPROVAL:
            # 需要申请 → 审批
            return False, "approval_required"

        if policy == JoinPolicy.INVITE_ONLY:
            return False, "invite_only"

        if policy == JoinPolicy.TOKEN:
            if config.join_token and token == config.join_token:
                return True, "valid_token"
            return False, "invalid_or_missing_token"

        return False, f"unknown_policy: {policy}"

    # ─────────── 申请 ───────────

    @property
    def requests_dir(self) -> Path:
        return self.workspace / DEFAULT_REQUESTS_DIR

    def request_join(
        self,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        backend_id: str = "unknown",
        hostname: str = "",
        pid: int = 0,
        message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        token: str = "",
    ) -> JoinRequest:
        """
        Agent 申请加入团队。

        Raises:
            PermissionError: 如果 join_policy 不兼容
            ValueError: 如果已存在 pending 申请
        """
        config = self.load_config()

        # 已是成员？直接返回
        if agent_id in config.member_ids:
            # 返回一个 mock request（已批准）
            return JoinRequest(
                agent_id=agent_id,
                capabilities=capabilities or [],
                groups=groups or [],
                backend_id=backend_id,
                hostname=hostname,
                pid=pid,
                message="already member",
                status=RequestStatus.APPROVED,
                reviewed_by="system",
                reviewed_at=datetime.now().isoformat(),
            )

        # 检查是否已有 pending 申请
        existing = self.get_request(agent_id)
        if existing and existing.status == RequestStatus.PENDING:
            raise ValueError(
                f"Agent '{agent_id}' already has a pending join request. "
                f"Wait for approval or cancel first."
            )

        # Token 模式：检查 token
        if config.join_policy == JoinPolicy.TOKEN:
            if not token or token != config.join_token:
                raise PermissionError(
                    f"join_policy=token requires a valid join_token."
                )

        # INVITE_ONLY 模式：拒绝申请
        if config.join_policy == JoinPolicy.INVITE_ONLY:
            raise PermissionError(
                f"join_policy=invite_only. "
                f"Agent '{agent_id}' can only be invited by an admin."
            )

        # 创建申请
        req = JoinRequest(
            agent_id=agent_id,
            capabilities=capabilities or [],
            groups=groups or [],
            backend_id=backend_id,
            hostname=hostname,
            pid=pid,
            message=message,
            status=RequestStatus.PENDING,
            metadata=metadata or {},
        )

        self._write_request(req)

        # 如果是 token 模式且 token 正确 → 自动批准
        if config.join_policy == JoinPolicy.TOKEN:
            return self.approve(req.agent_id, reviewed_by="system", note="token auto-approve")

        return req

    def get_request(self, agent_id: str) -> Optional[JoinRequest]:
        """获取某个 agent 的申请状态"""
        path = self._request_path_for(agent_id)
        if not path.exists():
            return None
        try:
            return JoinRequest.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    def list_requests(
        self,
        status: Optional[RequestStatus] = None,
    ) -> List[JoinRequest]:
        """列出所有申请（可按状态过滤）"""
        reqs = []
        req_dir = self.requests_dir
        if not req_dir.exists():
            return reqs
        for f in sorted(req_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                req = JoinRequest.from_dict(data)
                if status is None or req.status == status:
                    reqs.append(req)
            except Exception:
                continue
        return reqs

    def list_pending(self) -> List[JoinRequest]:
        """仅列出待审批的申请"""
        return self.list_requests(status=RequestStatus.PENDING)

    # ─────────── 审批 ───────────

    def approve(
        self,
        agent_id: str,
        reviewed_by: str = "",
        note: str = "",
    ) -> JoinRequest:
        """
        批准 agent 加入团队。

        效果：
        1. 申请状态 → approved
        2. 添加到 team.json member_ids
        3. 返回 updated JoinRequest
        """
        req = self.get_request(agent_id)
        if req is None:
            raise ValueError(f"No join request found for '{agent_id}'")

        if req.status != RequestStatus.PENDING:
            raise ValueError(
                f"Request for '{agent_id}' is already {req.status.value}"
            )

        # 更新申请
        req.status = RequestStatus.APPROVED
        req.reviewed_by = reviewed_by
        req.reviewed_at = datetime.now().isoformat()
        req.review_note = note or "approved"
        self._write_request(req)

        # 添加到团队 member list
        config = self.load_config()
        if agent_id not in config.member_ids:
            config.member_ids.append(agent_id)
        self.save_config(config)

        return req

    def reject(
        self,
        agent_id: str,
        reviewed_by: str = "",
        note: str = "",
    ) -> JoinRequest:
        """
        拒绝 agent 的加入申请。
        """
        req = self.get_request(agent_id)
        if req is None:
            raise ValueError(f"No join request found for '{agent_id}'")

        if req.status != RequestStatus.PENDING:
            raise ValueError(
                f"Request for '{agent_id}' is already {req.status.value}"
            )

        req.status = RequestStatus.REJECTED
        req.reviewed_by = reviewed_by
        req.reviewed_at = datetime.now().isoformat()
        req.review_note = note or "rejected"
        self._write_request(req)

        return req

    # ─────────── 邀请（INVITE_ONLY 模式） ───────────

    def invite(
        self,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        invited_by: str = "",
        note: str = "",
    ) -> JoinRequest:
        """
        管理员邀请 agent 加入（INVITE_ONLY 模式）。

        创建一个已批准的特殊申请，相当于直接添加成员。
        """
        config = self.load_config()

        # 已存在 pending 申请且是指定 agent
        existing = self.get_request(agent_id)
        if existing and existing.status == RequestStatus.PENDING:
            # 直接批准
            return self.approve(agent_id, reviewed_by=invited_by, note=note)

        # 创建已批准的申请记录
        import socket
        req = JoinRequest(
            agent_id=agent_id,
            capabilities=capabilities or [],
            groups=groups or [],
            backend_id="unknown",
            hostname="",
            pid=0,
            message=f"Invited by {invited_by}" + (f": {note}" if note else ""),
            status=RequestStatus.APPROVED,
            reviewed_by=invited_by,
            reviewed_at=datetime.now().isoformat(),
            review_note=note,
        )
        self._write_request(req)

        # 加入 member list
        if agent_id not in config.member_ids:
            config.member_ids.append(agent_id)
        self.save_config(config)

        return req

    def remove_member(self, agent_id: str) -> None:
        """移除成员"""
        config = self.load_config()
        if agent_id in config.member_ids:
            config.member_ids.remove(agent_id)
        self.save_config(config)

    # ─────────── 内部 ───────────

    def _request_path_for(self, agent_id: str) -> Path:
        safe_id = "".join(
            c if c.isalnum() or c in "_-." else "-" for c in agent_id
        )
        return self.requests_dir / f"{safe_id}.json"

    def _write_request(self, req: JoinRequest) -> None:
        path = self._request_path_for(req.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(req.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))

    # ─────────── Dashboard / 状态一览 ───────────

    def dashboard(self) -> str:
        """生成审批面板文本"""
        config = self.load_config()
        pending = self.list_pending()

        lines = [
            "=" * 50,
            f"  🏠 Team: {config.team_name} ({config.team_id})",
            f"  📋 Policy: {config.join_policy.value}",
            f"  👑 Admins: {', '.join(config.admin_ids) or '(none)'}",
            f"  👥 Members ({len(config.member_ids)}): {', '.join(config.member_ids[:10])}"
            + ("..." if len(config.member_ids) > 10 else ""),
            "=" * 50,
        ]

        if pending:
            lines.append(f"\n  📩 Pending Requests ({len(pending)}):")
            for r in pending:
                caps = ",".join(r.capabilities[:3])
                lines.append(
                    f"    [{r.request_id}] {r.agent_id}"
                    f"  caps=[{caps}]  "
                    f"  submitted={r.submitted_at[:16]}"
                )
                if r.message:
                    lines.append(f"         💬 \"{r.message}\"")
        else:
            lines.append(f"\n  📩 No pending requests.")

        # 最近的审批记录
        recent = [r for r in self.list_requests() if r.status != RequestStatus.PENDING]
        recent.sort(key=lambda r: r.reviewed_at, reverse=True)
        if recent:
            lines.append(f"\n  📜 Recent Decisions:")
            for r in recent[:5]:
                emoji = "✅" if r.status == RequestStatus.APPROVED else "❌"
                lines.append(
                    f"    {emoji} {r.agent_id} → {r.status.value} "
                    f"by {r.reviewed_by or '?'}  {r.reviewed_at[:16]}"
                )

        return "\n".join(lines)
