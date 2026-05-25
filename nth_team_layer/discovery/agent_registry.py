"""
AgentRegistry — 每个 Agent 的心跳与元数据注册

设计：
- 每个 Agent 在 team_agents/ 目录写 1 个 JSON 文件
- 文件名：{agent_id}.json（同 agent_id 重启会覆盖；不同 agent_id 不冲突）
- 内容：capabilities, backend, status, hostname, pid, last_seen, etc.
- 心跳：register() 时写 + 每隔 heartbeat_interval 秒由后台线程刷新
- Git 同步：team_agents/ 可被 PR 5 git_sync 同步到团队仓库

为什么是文件而非中心服务：
- 与 PR 5 设计一致（零冲突 append-only）
- 跨进程/跨终端通过文件系统或 Git 同步
- 无网络依赖，离线也能工作
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# 默认根目录：repo_root/team_agents/
DEFAULT_AGENTS_DIR = "team_agents"


@dataclass
class AgentRecord:
    """单个 Agent 的注册记录"""
    agent_id: str
    hostname: str
    pid: int
    backend_id: str = "unknown"        # mock / hermes / claude_code / ...
    capabilities: List[str] = field(default_factory=list)  # e.g. ["python", "web", "codegen"]
    groups: List[str] = field(default_factory=list)        # e.g. ["frontend", "ops"]
    status: str = "idle"                # idle / busy / blocked / offline
    current_mission: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    registered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())
    instance_token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRecord":
        # 忽略未知字段，避免上游字段升级时崩
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})

    def is_alive(self, max_stale_seconds: int = 90) -> bool:
        """根据 last_seen 判断是否活跃"""
        try:
            last = datetime.fromisoformat(self.last_seen)
            return (datetime.now() - last).total_seconds() < max_stale_seconds
        except Exception:
            return False

    def short(self) -> str:
        alive = "🟢" if self.is_alive() else "🔴"
        cap_str = ",".join(self.capabilities[:3]) or "-"
        return (
            f"{alive} {self.agent_id:20s} backend={self.backend_id:12s} "
            f"caps=[{cap_str}] status={self.status}"
        )


class AgentRegistry:
    """Agent 注册中心 — 心跳 + 状态广播"""

    DEFAULT_HEARTBEAT_INTERVAL = 30
    STALE_THRESHOLD_SECONDS = 90  # last_seen > 90s 视为离线

    def __init__(
        self,
        agents_dir: str = DEFAULT_AGENTS_DIR,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        """
        Args:
            agents_dir: 共享注册目录（默认 ./team_agents/，会被 PR 5 git_sync 拾取）
            heartbeat_interval: 后台心跳刷新间隔（秒）
        """
        self.agents_dir = Path(agents_dir)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval = heartbeat_interval

        self._record: Optional[AgentRecord] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    # ─────────────────── 写：注册 / 更新 / 注销 ───────────────────

    def register(
        self,
        agent_id: str,
        backend_id: str = "unknown",
        capabilities: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        start_heartbeat: bool = True,
    ) -> AgentRecord:
        """注册本 Agent，启动心跳线程"""
        # 如果已经注册，先注销
        if self._record is not None:
            self.unregister()

        self._record = AgentRecord(
            agent_id=agent_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            backend_id=backend_id,
            capabilities=capabilities or [],
            groups=groups or [],
            metadata=metadata or {},
        )
        self._write(self._record)

        if start_heartbeat:
            self._start_heartbeat()

        # 进程退出时自动注销
        atexit.register(self.unregister)

        return self._record

    def update_status(
        self,
        status: Optional[str] = None,
        current_mission: Optional[str] = None,
        metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        """更新本 Agent 的运行时状态（写盘 + 触发心跳）"""
        if self._record is None:
            raise RuntimeError("call register() first")
        if status is not None:
            self._record.status = status
        if current_mission is not None:
            self._record.current_mission = current_mission
        if metadata_patch:
            self._record.metadata.update(metadata_patch)
        self._record.last_seen = datetime.now().isoformat()
        self._write(self._record)

    def heartbeat(self) -> None:
        """单次心跳：刷新 last_seen 并落盘"""
        if self._record is None:
            return
        self._record.last_seen = datetime.now().isoformat()
        self._write(self._record)

    def unregister(self) -> None:
        """注销：停止心跳，删除心跳文件"""
        self._stop_heartbeat()
        if self._record is None:
            return
        target = self._path_for(self._record.agent_id)
        try:
            if target.exists():
                # 写最后一条 status=offline，便于审计；然后 unlink
                self._record.status = "offline"
                self._record.last_seen = datetime.now().isoformat()
                self._write(self._record)
                # 不立即 unlink — 让其他终端能看到 "下线" 状态一段时间
                # 后续 cleanup() 会清掉过期文件
        except Exception:
            pass
        self._record = None

    # ─────────────────── 读：列出 / 查询 ───────────────────

    def list_all(self) -> List[AgentRecord]:
        """读取所有注册记录"""
        records = []
        for f in sorted(self.agents_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                records.append(AgentRecord.from_dict(data))
            except Exception:
                continue
        return records

    def list_alive(self, max_stale: int = STALE_THRESHOLD_SECONDS) -> List[AgentRecord]:
        """只返回心跳活跃的 Agent"""
        return [r for r in self.list_all() if r.is_alive(max_stale)]

    def get(self, agent_id: str) -> Optional[AgentRecord]:
        path = self._path_for(agent_id)
        if not path.exists():
            return None
        try:
            return AgentRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    @property
    def my_record(self) -> Optional[AgentRecord]:
        return self._record

    # ─────────────────── 维护 ───────────────────

    def cleanup(self, max_stale: int = 86400) -> int:
        """删除超过 max_stale 秒的死亡记录（默认 1 天）"""
        removed = 0
        for f in self.agents_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                rec = AgentRecord.from_dict(data)
                if not rec.is_alive(max_stale):
                    f.unlink()
                    removed += 1
            except Exception:
                continue
        return removed

    # ─────────────────── 内部 ───────────────────

    def _path_for(self, agent_id: str) -> Path:
        # 文件名仅用 agent_id（同 agent_id 不同进程会互相覆盖 — 这是设计意图）
        safe_id = "".join(c if c.isalnum() or c in "_-." else "-" for c in agent_id)
        return self.agents_dir / f"{safe_id}.json"

    def _write(self, record: AgentRecord) -> None:
        path = self._path_for(record.agent_id)
        # 原子写入：先写 .tmp 再 rename
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()

        def _loop():
            while not self._heartbeat_stop.wait(self.heartbeat_interval):
                try:
                    self.heartbeat()
                except Exception:
                    pass  # 心跳失败不应崩溃 Agent

        self._heartbeat_thread = threading.Thread(
            target=_loop, daemon=True, name=f"AgentHeartbeat-{self._record.agent_id}"
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_thread is None:
            return
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=2)
        self._heartbeat_thread = None
