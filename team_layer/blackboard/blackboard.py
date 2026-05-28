"""
Blackboard

Append-only JSON Lines
-  scope shared.jsonl / group_*.jsonl / private_*.jsonl
-  entry_id  update
- get(entry_id) / list()

status
    todo / doing / done / blocked
     Kanban
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


#  append-only  + OS
_FILE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


@dataclass
class BlackboardEntry:
    """"""
    id: str
    scope: str          # str 'shared' / 'group:frontend' / 'private:alice'
    author: str         # agent_id  username
    topic: str          #
    status: str         # todo / doing / done / blocked /
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1    #  id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BlackboardEntry":
        return cls(**data)

    def short(self) -> str:
        return f"[{self.status:7s}] {self.topic} (by {self.author})"


class Blackboard:
    """ Agent """

    def __init__(self, root: Optional[Union[str, Path]] = None):
        """
        Args:
            root:  ./blackboard/
        """
        self.root = Path(root) if root else Path("blackboard")
        self.root.mkdir(parents=True, exist_ok=True)

    #

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


        Args:
            topic:
            author: agent_id  username
            scope: Scope
            status:  todo
            content:
            metadata:
            entry_id:  id uuid4

        Returns:
             BlackboardEntry
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
        append

        scope  entry  group:frontendupdate  group:frontend
                   entry_id
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

    #

    def get(
        self,
        entry_id: str,
        scope: Optional[Union[Scope, str]] = None,
    ) -> Optional[BlackboardEntry]:
        """"""
        if scope is None:
            entry, _ = self._find_entry_by_id(entry_id)
            return entry

        scope = self._coerce_scope(scope)
        entries = self._read_scope(scope)
        #
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


        Args:
            scope: None =  .jsonl
            status:
            author:
            topic_contains: topic
        """
        if scope is not None:
            scope = self._coerce_scope(scope)
            scopes = [scope]
        else:
            scopes = self._discover_scopes()

        #
        all_entries: Dict[str, BlackboardEntry] = {}
        for sc in scopes:
            for e in self._read_scope(sc):
                #  id
                if e.id not in all_entries or e.version > all_entries[e.id].version:
                    all_entries[e.id] = e

        results = list(all_entries.values())

        #
        if status is not None:
            results = [e for e in results if e.status == status]
        if author is not None:
            results = [e for e in results if e.author == author]
        if topic_contains is not None:
            results = [e for e in results if topic_contains.lower() in e.topic.lower()]

        #  updated_at
        results.sort(key=lambda e: e.updated_at, reverse=True)
        return results

    def history(self, entry_id: str) -> List[BlackboardEntry]:
        """ version """
        for sc in self._discover_scopes():
            versions = [e for e in self._read_scope(sc) if e.id == entry_id]
            if versions:
                versions.sort(key=lambda e: e.version)
                return versions
        return []

    #

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
                os.fsync(f.fileno())  #

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
        """ root  .jsonl  Scope"""
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
        """ scope  entry (entry, scope)  (None, None)"""
        for sc in self._discover_scopes():
            for e in self._read_scope(sc):
                if e.id == entry_id:
                    latest = self.get(entry_id, sc)
                    return latest, sc
        return None, None
