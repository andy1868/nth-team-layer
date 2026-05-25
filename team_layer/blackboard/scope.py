"""
Scope — 黑板作用域

格式：
  shared                — 全团队
  group:<name>          — 子团队（如 group:frontend）
  private:<agent_id>    — 单 Agent 私有

设计：
- Scope 实例不可变，便于作为字典 key
- 提供文件路径派生（每个 scope 对应一个 .jsonl）
- 提供 git_sync_eligible 判断（private 永不进 Git）
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
    """作用域 — 不可变值对象"""
    kind: ScopeKind
    name: str = ""  # 仅 group/private 时使用

    @classmethod
    def parse(cls, raw: str) -> "Scope":
        """从字符串解析作用域"""
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
        """对应的 .jsonl 文件名（同目录下）"""
        if self.kind == ScopeKind.SHARED:
            return "shared.jsonl"
        elif self.kind == ScopeKind.GROUP:
            return f"group_{self.name}.jsonl"
        else:  # private
            return f"private_{self.name}.jsonl"

    def git_sync_eligible(self) -> bool:
        """是否参与 Git 同步（private 永远不进 Git）"""
        return self.kind != ScopeKind.PRIVATE

    def allows_writer(self, author: str, owner_hint: Optional[str] = None) -> bool:
        """
        简单的写入权限规则：
        - shared/group：任何 Agent 可写
        - private：只有匹配 agent_id 的 Agent 可写
        """
        if self.kind == ScopeKind.PRIVATE:
            # private:alice 只允许 author=alice 写
            return author == self.name or (owner_hint and owner_hint == self.name)
        return True
