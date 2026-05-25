"""
SyncConfig — 跨平台同步配置

职责：
- 探测 hostname / username（跨 Linux/macOS/Windows）
- 标准化 Git 路径（repo root、logs/、skills/、sidechain/）
- 提供 author 标识（commit 时使用）
- 防御性默认值（在 CI/Docker 等无 user 环境也能工作）
"""

import getpass
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def detect_hostname() -> str:
    """探测 hostname（多重降级）"""
    # 优先环境变量（CI 友好）
    for env_var in ("HOSTNAME", "COMPUTERNAME", "HOST"):
        value = os.environ.get(env_var)
        if value:
            return _sanitize(value)
    try:
        return _sanitize(socket.gethostname())
    except Exception:
        return "unknown-host"


def detect_username() -> str:
    """探测当前用户名（跨平台 + CI 友好）"""
    # GitHub Actions / CI 优先用 actor
    for env_var in ("GITHUB_ACTOR", "CI_COMMIT_AUTHOR", "USER", "USERNAME"):
        value = os.environ.get(env_var)
        if value:
            return _sanitize(value)
    try:
        return _sanitize(getpass.getuser())
    except Exception:
        return "unknown-user"


def detect_repo_root(start: Optional[Path] = None) -> Path:
    """探测 git 仓库根目录（从指定目录向上查找 .git）"""
    start = start or Path.cwd()
    # 优先调 git
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    # 降级：手动向上查找
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start.resolve()


def _sanitize(value: str) -> str:
    """清理用于文件名的字符串（去除非法字符）"""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", value).strip("-") or "unknown"


@dataclass
class SyncConfig:
    """多终端同步配置"""
    repo_root: Path = field(default_factory=detect_repo_root)
    hostname: str = field(default_factory=detect_hostname)
    username: str = field(default_factory=detect_username)
    branch: str = "team-layer-v1"

    # 路径（相对 repo_root）
    # 注意：用 team_logs/ 而非 logs/，因为 Hermes 上游已 gitignore 了 logs/
    logs_dir: str = "team_logs"
    skills_dir: str = "skills"
    sidechain_dir: str = "sidechain"
    ledger_path: str = "sidechain/ledger.jsonl"
    sync_audit_path: str = "sidechain/sync_audit.jsonl"
    reload_signal_path: str = "sidechain/reload.signal"

    # 同步策略
    auto_push: bool = True
    push_remote: str = "origin"

    # 安全：永不 push 的路径模式（防敏感泄漏）
    forbidden_paths: tuple = (
        ".env", ".env.*",
        "*.key", "*.pem", "*.p12",
        "credentials.json", "secrets.json",
        "memory/*.db", "memory/*.jsonl",
        ".idea/", ".vscode/", "__pycache__/",
    )

    def __post_init__(self):
        self.repo_root = Path(self.repo_root).resolve()

    # —— 路径工厂方法 ——
    def logs_path(self) -> Path:
        return self.repo_root / self.logs_dir

    def skills_path(self) -> Path:
        return self.repo_root / self.skills_dir

    def sidechain_path(self) -> Path:
        return self.repo_root / self.sidechain_dir

    def ledger_full_path(self) -> Path:
        return self.repo_root / self.ledger_path

    def sync_audit_full_path(self) -> Path:
        return self.repo_root / self.sync_audit_path

    def reload_signal_full_path(self) -> Path:
        return self.repo_root / self.reload_signal_path

    # —— 命名规则 ——
    def make_log_filename(self, timestamp: Optional[int] = None) -> str:
        """生成零冲突日志文件名：{hostname}_{username}_{timestamp}.jsonl"""
        import time
        ts = timestamp if timestamp is not None else int(time.time())
        return f"{self.hostname}_{self.username}_{ts}.jsonl"

    def make_commit_author(self) -> str:
        """生成 commit 作者标识（多终端可识别）"""
        return f"TeamAgent on {self.hostname} ({self.username})"

    def is_forbidden(self, rel_path: str) -> bool:
        """判断路径是否在禁止 push 名单中"""
        from fnmatch import fnmatch
        for pattern in self.forbidden_paths:
            if fnmatch(rel_path, pattern):
                return True
            # 目录前缀匹配
            if pattern.endswith("/") and rel_path.startswith(pattern):
                return True
        return False

    def describe(self) -> str:
        return (
            f"SyncConfig(host={self.hostname}, user={self.username}, "
            f"repo={self.repo_root.name}, branch={self.branch})"
        )
