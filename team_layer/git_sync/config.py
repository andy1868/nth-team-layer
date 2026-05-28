"""
SyncConfig


-  hostname / username Linux/macOS/Windows
-  Git repo rootlogs/skills/sidechain/
-  author commit
-  CI/Docker  user
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
    """ hostname"""
    # CI
    for env_var in ("HOSTNAME", "COMPUTERNAME", "HOST"):
        value = os.environ.get(env_var)
        if value:
            return _sanitize(value)
    try:
        return _sanitize(socket.gethostname())
    except Exception:
        return "unknown-host"


def detect_username() -> str:
    """ + CI """
    # GitHub Actions / CI  actor
    for env_var in ("GITHUB_ACTOR", "CI_COMMIT_AUTHOR", "USER", "USERNAME"):
        value = os.environ.get(env_var)
        if value:
            return _sanitize(value)
    try:
        return _sanitize(getpass.getuser())
    except Exception:
        return "unknown-user"


def detect_repo_root(start: Optional[Path] = None) -> Path:
    """ git  .git"""
    start = start or Path.cwd()
    #  git
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

    #
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return start.resolve()


def _sanitize(value: str) -> str:
    """"""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", value).strip("-") or "unknown"


@dataclass
class SyncConfig:
    """"""
    repo_root: Path = field(default_factory=detect_repo_root)
    hostname: str = field(default_factory=detect_hostname)
    username: str = field(default_factory=detect_username)
    branch: str = "nth-dao-main"

    #  repo_root
    #  team_logs/  logs/ Hermes  gitignore  logs/
    logs_dir: str = "team_logs"
    skills_dir: str = "skills"
    sidechain_dir: str = "sidechain"
    ledger_path: str = "sidechain/ledger.jsonl"
    sync_audit_path: str = "sidechain/sync_audit.jsonl"
    reload_signal_path: str = "sidechain/reload.signal"

    #
    auto_push: bool = True
    push_remote: str = "origin"

    #  push
    forbidden_paths: tuple = (
        ".env", ".env.*",
        "*.key", "*.pem", "*.p12",
        "credentials.json", "secrets.json",
        "memory/*.db", "memory/*.jsonl",
        ".idea/", ".vscode/", "__pycache__/",
    )

    def __post_init__(self):
        self.repo_root = Path(self.repo_root).resolve()

    #
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

    #
    def make_log_filename(self, timestamp: Optional[int] = None) -> str:
        """{hostname}_{username}_{timestamp}.jsonl"""
        import time
        ts = timestamp if timestamp is not None else int(time.time())
        return f"{self.hostname}_{self.username}_{ts}.jsonl"

    def make_commit_author(self) -> str:
        """ commit """
        return f"TeamAgent on {self.hostname} ({self.username})"

    def is_forbidden(self, rel_path: str) -> bool:
        """ push """
        from fnmatch import fnmatch
        for pattern in self.forbidden_paths:
            if fnmatch(rel_path, pattern):
                return True
            #
            if pattern.endswith("/") and rel_path.startswith(pattern):
                return True
        return False

    def describe(self) -> str:
        return (
            f"SyncConfig(host={self.hostname}, user={self.username}, "
            f"repo={self.repo_root.name}, branch={self.branch})"
        )
