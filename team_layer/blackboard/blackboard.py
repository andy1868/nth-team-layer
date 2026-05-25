"""
Blackboard — 核心实现

Append-only JSON Lines 存储：
- 每个 scope 一个文件（shared.jsonl / group_*.jsonl / private_*.jsonl）
- 同一 entry_id 的多次 update 都追加（保留完整历史）
- get(entry_id) / list() 自动返回最新版本

状态字段（status）：
    todo / doing / done / blocked
    其他自定义状态也允许，但 Kanban 视图只展示前三类
"""

import json
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .scope import Scope, ScopeKind


# 文件锁（同进程内并发保护；跨进程靠 append-only 模式 + OS 原子追加）
_FILE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


@dataclass
class BlackboardEntry:
    """黑板条目（不可变快照）"""
    id: str
    scope: str          # str 形式：'shared' / 'group:frontend' / 'private:alice'
    author: str         # agent_id 或 username
    topic: str          # 简短标题
    status: str         # todo / doing / done / blocked / 自定义
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1    # 同 id 的第几个版本

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BlackboardEntry":
        return cls(**data)

    def short(self) -> str:
        return f"[{self.status:7s}] {self.topic} (by {self.author})"


class Blackboard:
    """多 Agent 共享数据空间"""

    def __init__(self, root: Optional[Union[str, Path]] = None):
        """
        Args:
            root: 黑板根目录（默认 ./blackboard/）
        """
        self.root = Path(root) if root else Path("blackboard")
        self.root.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────── 写入 ───────────────────────────

    def post(
        self,
        topic: str,
        author: str,
        scope: Union[Scope, str] = "shared",
        status: str = "todo",
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        entry_id: Optional[str] = None,
    ) -> BlackboardEntry:
        """
        发布一条新条目。

        Args:
            topic: 简短标题（必填）
            author: 作者标识（agent_id 或 username）
            scope: 作用域（Scope 实例或字符串）
            status: 初始状态（默认 todo）
            content: 详细内容
            metadata: 附加字段
            entry_id: 可选指定 id（不指定则自动生成 uuid4）

        Returns:
            发布的 BlackboardEntry
        """
        scope = self._coerce_scope(scope)
        self._check_writable(scope, author)

        entry = BlackboardEntry(
            id=entry_id or self._generate_id(),
            scope=str(scope),
            author=author,
            topic=topic,
            status=status,
            content=content,
            metadata=metadata or {},
            version=1,
        )
        self._append(scope, entry)
        return entry

    def update(
        self,
        entry_id: str,
        author: str,
        status: Optional[str] = None,
        content: Optional[str] = None,
        topic: Optional[str] = None,
        metadata_patch: Optional[Dict[str, Any]] = None,
        scope: Optional[Union[Scope, str]] = None,
    ) -> BlackboardEntry:
        """
        更新条目（append 新版本，旧版本保留）。

        scope 可选：如果原 entry 在 group:frontend，update 时也写到 group:frontend。
                  如未指定，自动通过 entry_id 反查。
        """
        if scope is not None:
            scope = self._coerce_scope(scope)
            previous = self.get(entry_id, scope)
        else:
            previous, scope = self._find_entry_by_id(entry_id)
            if previous is None:
                raise ValueError(f"entry_id {entry_id!r} not found in any scope")

        self._check_writable(scope, author)

        new_metadata = dict(previous.metadata)
        if metadata_patch:
            new_metadata.update(metadata_patch)

        new_entry = BlackboardEntry(
            id=entry_id,
            scope=str(scope),
            author=author,
            topic=topic if topic is not None else previous.topic,
            status=status if status is not None else previous.status,
            content=content if content is not None else previous.content,
            metadata=new_metadata,
            created_at=previous.created_at,
            updated_at=datetime.now().isoformat(),
            version=previous.version + 1,
        )
        self._append(scope, new_entry)
        return new_entry

    # ─────────────────────────── 读取 ───────────────────────────

    def get(
        self,
        entry_id: str,
        scope: Optional[Union[Scope, str]] = None,
    ) -> Optional[BlackboardEntry]:
        """获取条目的最新版本"""
        if scope is None:
            entry, _ = self._find_entry_by_id(entry_id)
            return entry

        scope = self._coerce_scope(scope)
        entries = self._read_scope(scope)
        # 找最新版本
        latest = None
        for e in entries:
            if e.id == entry_id:
                if latest is None or e.version > latest.version:
                    latest = e
        return latest

    def list(
        self,
        scope: Optional[Union[Scope, str]] = None,
        status: Optional[str] = None,
        author: Optional[str] = None,
        topic_contains: Optional[str] = None,
    ) -> List[BlackboardEntry]:
        """
        列出符合条件的条目（已去重为最新版本）。

        Args:
            scope: 限定作用域（None = 扫描所有 .jsonl）
            status: 过滤状态
            author: 过滤作者
            topic_contains: topic 子串匹配
        """
        if scope is not None:
            scope = self._coerce_scope(scope)
            scopes = [scope]
        else:
            scopes = self._discover_scopes()

        # 收集所有条目
        all_entries: Dict[str, BlackboardEntry] = {}
        for sc in scopes:
            for e in self._read_scope(sc):
                # 同 id 保留最新版本
                if e.id not in all_entries or e.version > all_entries[e.id].version:
                    all_entries[e.id] = e

        results = list(all_entries.values())

        # 过滤
        if status is not None:
            results = [e for e in results if e.status == status]
        if author is not None:
            results = [e for e in results if e.author == author]
        if topic_contains is not None:
            results = [e for e in results if topic_contains.lower() in e.topic.lower()]

        # 按 updated_at 倒序
        results.sort(key=lambda e: e.updated_at, reverse=True)
        return results

    def history(self, entry_id: str) -> List[BlackboardEntry]:
        """返回某条目的完整版本历史（按 version 升序）"""
        for sc in self._discover_scopes():
            versions = [e for e in self._read_scope(sc) if e.id == entry_id]
            if versions:
                versions.sort(key=lambda e: e.version)
                return versions
        return []

    # ─────────────────────────── 内部 ───────────────────────────

    @staticmethod
    def _coerce_scope(scope: Union[Scope, str]) -> Scope:
        if isinstance(scope, Scope):
            return scope
        return Scope.parse(scope)

    @staticmethod
    def _check_writable(scope: Scope, author: str) -> None:
        if not scope.allows_writer(author):
            raise PermissionError(
                f"author {author!r} cannot write to {scope} "
                f"(private scope requires matching agent_id)"
            )

    @staticmethod
    def _generate_id() -> str:
        return uuid.uuid4().hex[:12]

    def _scope_path(self, scope: Scope) -> Path:
        return self.root / scope.filename()

    def _append(self, scope: Scope, entry: BlackboardEntry) -> None:
        path = self._scope_path(scope)
        lock = _get_lock(str(path))
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())  # 确保多进程可见

    def _read_scope(self, scope: Scope) -> List[BlackboardEntry]:
        path = self._scope_path(scope)
        if not path.exists():
            return []
        results = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    results.append(BlackboardEntry.from_dict(data))
                except (json.JSONDecodeError, TypeError):
                    continue
        return results

    def _discover_scopes(self) -> List[Scope]:
        """扫描 root 下所有 .jsonl 文件还原为 Scope"""
        scopes: List[Scope] = []
        if not self.root.exists():
            return scopes
        for f in sorted(self.root.glob("*.jsonl")):
            name = f.stem
            try:
                if name == "shared":
                    scopes.append(Scope.shared())
                elif name.startswith("group_"):
                    scopes.append(Scope.group(name[6:]))
                elif name.startswith("private_"):
                    scopes.append(Scope.private(name[8:]))
            except Exception:
                continue
        return scopes

    def _find_entry_by_id(self, entry_id: str) -> tuple:
        """全 scope 查找 entry，返回 (entry, scope) 或 (None, None)"""
        for sc in self._discover_scopes():
            for e in self._read_scope(sc):
                if e.id == entry_id:
                    latest = self.get(entry_id, sc)
                    return latest, sc
        return None, None
