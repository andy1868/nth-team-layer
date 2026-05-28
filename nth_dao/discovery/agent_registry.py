"""
AgentRegistry   Agent


-  Agent  team_agents/  1  JSON
- {agent_id}.json agent_id  agent_id
- capabilities, backend, status, hostname, pid, last_seen, etc.
- register()  +  heartbeat_interval
- Git team_agents/  PR 5 git_sync


-  PR 5  append-only
- / Git
-
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


# repo_root/team_agents/
DEFAULT_AGENTS_DIR = "team_agents"


@dataclass
class AgentRecord:
    """ Agent """
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
        #
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})

    def is_alive(self, max_stale_seconds: int = 90) -> bool:
        """ last_seen """
        try:
            last = datetime.fromisoformat(self.last_seen)
            return (datetime.now() - last).total_seconds() < max_stale_seconds
        except Exception:
            return False

    def short(self) -> str:
        alive = "" if self.is_alive() else ""
        cap_str = ",".join(self.capabilities[:3]) or "-"
        return (
            f"{alive} {self.agent_id:20s} backend={self.backend_id:12s} "
            f"caps=[{cap_str}] status={self.status}"
        )


class AgentRegistry:
    """Agent    + """

    DEFAULT_HEARTBEAT_INTERVAL = 30
    STALE_THRESHOLD_SECONDS = 90  # last_seen > 90s

    def __init__(
        self,
        agents_dir: str = DEFAULT_AGENTS_DIR,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        """
        Args:
            agents_dir:  ./team_agents/ PR 5 git_sync
            heartbeat_interval:
        """
        self.agents_dir = Path(agents_dir)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval = heartbeat_interval

        self._record: Optional[AgentRecord] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    #   /  /

    def register(
        self,
        agent_id: str,
        backend_id: str = "unknown",
        capabilities: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        start_heartbeat: bool = True,
    ) -> AgentRecord:
        """ Agent"""
        #
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

        #
        atexit.register(self.unregister)

        return self._record

    def update_status(
        self,
        status: Optional[str] = None,
        current_mission: Optional[str] = None,
        metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        """ Agent  + """
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
        """ last_seen """
        if self._record is None:
            return
        self._record.last_seen = datetime.now().isoformat()
        self._write(self._record)

    def unregister(self) -> None:
        """"""
        self._stop_heartbeat()
        if self._record is None:
            return
        target = self._path_for(self._record.agent_id)
        try:
            if target.exists():
                #  status=offline unlink
                self._record.status = "offline"
                self._record.last_seen = datetime.now().isoformat()
                self._write(self._record)
                #  unlink   ""
                #  cleanup()
        except Exception:
            pass
        self._record = None

    #   /

    def list_all(self) -> List[AgentRecord]:
        """"""
        records = []
        for f in sorted(self.agents_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                records.append(AgentRecord.from_dict(data))
            except Exception:
                continue
        return records

    def list_alive(self, max_stale: int = STALE_THRESHOLD_SECONDS) -> List[AgentRecord]:
        """ Agent"""
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

    #

    def cleanup(self, max_stale: int = 86400) -> int:
        """ max_stale  1 """
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

    #

    def _path_for(self, agent_id: str) -> Path:
        #  agent_id agent_id
        safe_id = "".join(c if c.isalnum() or c in "_-." else "-" for c in agent_id)
        return self.agents_dir / f"{safe_id}.json"

    def _write(self, record: AgentRecord) -> None:
        path = self._path_for(record.agent_id)
        #  .tmp  rename
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
                    pass  #  Agent

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
