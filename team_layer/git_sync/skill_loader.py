"""
SkillLoader — 原子级技能热加载

核心思路（来自原始设计第 6.3 节）：
    git fetch origin main
    git checkout origin/main -- skills/ TEAM-SOUL.md
    # ↑ 仅更新 skills 目录，不切换分支、不动工作区其他文件
    if 成功:
        发送热加载信号 (pkill -HUP / 写信号文件)

为什么是"原子"：
- git checkout -- <paths> 只更新指定路径
- 失败时 git 不会留下半截文件（要么全成功，要么不动）
- 主代理在收到信号前不会读到中间状态

信号机制（跨平台）：
- POSIX：尝试 pkill -HUP；失败则降级写信号文件
- Windows：始终用信号文件（sidechain/reload.signal）
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import SyncConfig


@dataclass
class ReloadResult:
    """热加载结果"""
    success: bool
    fetched_from: str = ""
    updated_paths: List[str] = field(default_factory=list)
    signal_sent: bool = False
    signal_method: str = ""
    error: str = ""

    def __str__(self) -> str:
        if not self.success:
            return f"RELOAD [FAIL] — {self.error}"
        return (
            f"RELOAD [OK] from {self.fetched_from} → "
            f"{len(self.updated_paths)} path(s) updated, "
            f"signal={'sent via ' + self.signal_method if self.signal_sent else 'skipped'}"
        )


class SkillLoader:
    """原子级技能热加载器"""

    # 默认热加载的路径白名单（git checkout -- 的目标）
    # 仅技能与灵魂相关，绝不包含核心代码
    DEFAULT_RELOAD_PATHS = (
        "skills/",
        "memory/auto-memory.md",
    )

    def __init__(
        self,
        config: Optional[SyncConfig] = None,
        reload_paths: Optional[tuple] = None,
        agent_process_pattern: str = "team_entrypoint",
    ):
        """
        Args:
            config: SyncConfig
            reload_paths: 要热加载的路径列表（默认 skills/ + memory/auto-memory.md）
            agent_process_pattern: pkill -f 用的进程匹配模式
        """
        self.cfg = config or SyncConfig()
        self.reload_paths = reload_paths or self.DEFAULT_RELOAD_PATHS
        self.agent_process_pattern = agent_process_pattern

    def reload(self, send_signal: bool = True) -> ReloadResult:
        """执行原子级热加载"""
        # 1. fetch 远程最新
        try:
            self._git("fetch", self.cfg.push_remote, self.cfg.branch)
        except subprocess.CalledProcessError as e:
            err = f"fetch failed: {e.stderr[:200] if e.stderr else e}"
            self._audit("reload_failed", error=err)
            return ReloadResult(success=False, error=err)

        # 2. 原子 checkout 指定路径
        fetched_ref = f"{self.cfg.push_remote}/{self.cfg.branch}"
        updated = []
        for rel_path in self.reload_paths:
            try:
                result = self._git(
                    "checkout", fetched_ref, "--", rel_path, check=False
                )
                if result.returncode == 0:
                    updated.append(rel_path)
                else:
                    # 路径不存在等情况 — 跳过但不算失败
                    stderr_lower = (result.stderr or "").lower()
                    if "did not match" in stderr_lower or "pathspec" in stderr_lower:
                        continue
                    err = f"checkout {rel_path} failed: {result.stderr[:200]}"
                    self._audit("reload_failed", error=err, partial=updated)
                    return ReloadResult(
                        success=False,
                        fetched_from=fetched_ref,
                        updated_paths=updated,
                        error=err,
                    )
            except Exception as e:
                err = f"checkout exception: {e}"
                self._audit("reload_failed", error=err)
                return ReloadResult(success=False, error=err)

        if not updated:
            self._audit("reload_noop", reason="no matching paths")
            return ReloadResult(
                success=True,
                fetched_from=fetched_ref,
                error="no paths matched (nothing to reload)",
            )

        # 3. 发送热加载信号
        signal_sent = False
        signal_method = ""
        if send_signal:
            signal_sent, signal_method = self._send_reload_signal()

        self._audit(
            "reload_done",
            fetched_from=fetched_ref,
            updated_paths=updated,
            signal_sent=signal_sent,
            signal_method=signal_method,
        )
        return ReloadResult(
            success=True,
            fetched_from=fetched_ref,
            updated_paths=updated,
            signal_sent=signal_sent,
            signal_method=signal_method,
        )

    def _send_reload_signal(self) -> tuple:
        """跨平台发送热加载信号 → (sent: bool, method: str)"""
        # 优先：POSIX pkill -HUP
        if sys.platform != "win32":
            try:
                result = subprocess.run(
                    ["pkill", "-HUP", "-f", self.agent_process_pattern],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return True, "pkill -HUP"
            except FileNotFoundError:
                pass

        # 降级：写信号文件
        signal_path = self.cfg.reload_signal_full_path()
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "requested_at": datetime.now().isoformat(),
            "hostname": self.cfg.hostname,
            "username": self.cfg.username,
            "reload_paths": list(self.reload_paths),
        }
        signal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True, f"signal-file ({signal_path.name})"

    # —— Agent 端配合：检查 reload 信号 ——
    @staticmethod
    def check_reload_pending(config: Optional[SyncConfig] = None) -> Optional[dict]:
        """
        Agent 主循环调用此函数检查是否有待处理的 reload 信号。
        返回 dict 表示需要 reload；返回 None 表示无信号。
        消费后自动删除信号文件。
        """
        cfg = config or SyncConfig()
        signal_path = cfg.reload_signal_full_path()
        if not signal_path.exists():
            return None
        try:
            payload = json.loads(signal_path.read_text(encoding="utf-8"))
            signal_path.unlink()  # 消费掉
            return payload
        except Exception:
            return None

    def _git(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.cfg.repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )

    def _audit(self, action: str, **kwargs) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "hostname": self.cfg.hostname,
            "username": self.cfg.username,
            **kwargs,
        }
        audit_path = self.cfg.sync_audit_full_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[RELOAD] audit failed: {e}")
