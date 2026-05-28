"""
Scope


  shared
  group:<name>            group:frontend
  private:<agent_id>      Agent


- Scope  key
-  scope  .jsonl
-  git_sync_eligible private  Git
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class ScopeKind(str, Enum):
    SHARED = "shared"
    GROUP = "group"
    PRIVATE = "private"


_SCOPE_PATTERN = re.compile(r"^(shared|group:[a-zA-Z0-9_-]+|private:[a-zA-Z0-9_.-]+)$")


@dataclass(frozen=True)
class Scope:
    """  """
    kind: ScopeKind
    name: str = ""  #  group/private

    @classmethod
    def parse(cls, raw: str) -> "Scope":
        """"""
        if not raw:
            raise ValueError("scope cannot be empty")
        if not _SCOPE_PATTERN.match(raw):
            raise ValueError(
                f"invalid scope: {raw!r} "
                f"(expected 'shared', 'group:<name>', or 'private:<agent>')"
            )
        if raw == "shared":
            return cls(kind=ScopeKind.SHARED)
        kind_str, _, name = raw.partition(":")
        kind = ScopeKind(kind_str)
        return cls(kind=kind, name=name)

    @classmethod
    def shared(cls) -> "Scope":
        return cls(kind=ScopeKind.SHARED)

    @classmethod
    def group(cls, name: str) -> "Scope":
        return cls(kind=ScopeKind.GROUP, name=name)

    @classmethod
    def private(cls, agent_id: str) -> "Scope":
        return cls(kind=ScopeKind.PRIVATE, name=agent_id)

    def __str__(self) -> str:
        if self.kind == ScopeKind.SHARED:
            return "shared"
        return f"{self.kind.value}:{self.name}"

    def filename(self) -> str:
        """ .jsonl """
        if self.kind == ScopeKind.SHARED:
            return "shared.jsonl"
        elif self.kind == ScopeKind.GROUP:
            return f"group_{self.name}.jsonl"
        else:  # private
            return f"private_{self.name}.jsonl"

    def git_sync_eligible(self) -> bool:
        """ Git private  Git"""
        return self.kind != ScopeKind.PRIVATE

    def allows_writer(self, author: str, owner_hint: Optional[str] = None) -> bool:
        """

        - shared/group Agent
        - private agent_id  Agent
        """
        if self.kind == ScopeKind.PRIVATE:
            # private:alice  author=alice
            return author == self.name or (owner_hint and owner_hint == self.name)
        return True
