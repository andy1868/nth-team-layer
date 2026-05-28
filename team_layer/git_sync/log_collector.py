"""
LogCollector   + Git


1.  ledger  last_collected
2.  logs/{hostname}_{username}_{timestamp}.jsonl
3. git add  + commit + push
4.  sidechain/sync_audit.jsonl


-  git add -A / git add .
-  logs/
- push  audit
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import SyncConfig


@dataclass
class CollectResult:
    """"""
    success: bool
    log_file: Optional[str] = None
    entries_collected: int = 0
    committed: bool = False
    pushed: bool = False
    error: str = ""

    def __str__(self) -> str:
        if not self.success:
            return f"COLLECT [FAIL]  {self.error}"
        flags = []
        if self.committed: flags.append("committed")
        if self.pushed: flags.append("pushed")
        return f"COLLECT [OK] {self.entries_collected} entries  {self.log_file} ({', '.join(flags) or 'local-only'})"


class LogCollector:
    """"""

    def __init__(self, config: Optional[SyncConfig] = None):
        self.cfg = config or SyncConfig()
        self.cfg.logs_path().mkdir(parents=True, exist_ok=True)
        self.cfg.sidechain_path().mkdir(parents=True, exist_ok=True)

    def collect(
        self,
        since_timestamp: Optional[str] = None,
        auto_push: Optional[bool] = None,
    ) -> CollectResult:
        """
         ledger

        Args:
            since_timestamp: ISO  since last_collected
            auto_push:  config
        """
        # 1.
        try:
            entries = self._read_incremental(since_timestamp)
        except Exception as e:
            self._audit("collect_failed", error=str(e))
            return CollectResult(success=False, error=f"Read failed: {e}")

        if not entries:
            return CollectResult(success=True, entries_collected=0, error="no new entries")

        # 2.
        log_filename = self.cfg.make_log_filename()
        log_path = self.cfg.logs_path() / log_filename

        try:
            with open(log_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    #  aggregator
                    entry.setdefault("collected_by", {
                        "hostname": self.cfg.hostname,
                        "username": self.cfg.username,
                        "collected_at": datetime.now().isoformat(),
                    })
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self._audit("collect_failed", error=str(e))
            return CollectResult(success=False, error=f"Write failed: {e}")

        # 3.  last_collected
        self._mark_last_collected(datetime.now().isoformat())

        result = CollectResult(
            success=True,
            log_file=str(log_path.relative_to(self.cfg.repo_root)),
            entries_collected=len(entries),
        )

        # 4. Git add + commit + push
        do_push = auto_push if auto_push is not None else self.cfg.auto_push
        if do_push:
            self._commit_and_push(log_path, result)

        self._audit(
            "collect_done",
            log_file=result.log_file,
            entries=len(entries),
            committed=result.committed,
            pushed=result.pushed,
        )
        return result

    #

    def _read_incremental(self, since_iso: Optional[str]) -> List[dict]:
        """ ledger since_iso """
        ledger = self.cfg.ledger_full_path()
        if not ledger.exists():
            return []

        cutoff = since_iso or self._load_last_collected()
        results = []
        with open(ledger, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                #
                if cutoff and entry.get("timestamp", "") <= cutoff:
                    continue
                results.append(entry)
        return results

    def _commit_and_push(self, log_path: Path, result: CollectResult) -> None:
        """ git add  + commit + push"""
        rel_path = str(log_path.relative_to(self.cfg.repo_root)).replace("\\", "/")

        #
        if self.cfg.is_forbidden(rel_path):
            result.error = f"refused: {rel_path} in forbidden_paths"
            return

        #  logs/
        if not rel_path.startswith(self.cfg.logs_dir + "/"):
            result.error = f"refused: {rel_path} not under {self.cfg.logs_dir}/"
            return

        try:
            # git add  -A  .
            self._git("add", "--", rel_path)
            # commit
            commit_msg = (
                f"log: {self.cfg.hostname}/{self.cfg.username} "
                f"+{result.entries_collected} entries"
            )
            commit_out = self._git("commit", "-m", commit_msg, check=False)
            if commit_out.returncode == 0:
                result.committed = True
            elif "nothing to commit" in (commit_out.stdout + commit_out.stderr).lower():
                #  commit
                result.committed = False
                return
            else:
                result.error = f"commit failed: {commit_out.stderr[:200]}"
                return

            # push
            push_out = self._git(
                "push", self.cfg.push_remote, self.cfg.branch, check=False
            )
            if push_out.returncode == 0:
                result.pushed = True
            else:
                result.error = f"push failed: {push_out.stderr[:200]}"
        except Exception as e:
            result.error = f"git op failed: {e}"

    def _git(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        """ git  cwd  repo_root"""
        return subprocess.run(
            ["git", *args],
            cwd=str(self.cfg.repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )

    def _load_last_collected(self) -> str:
        """ collect """
        marker = self.cfg.sidechain_path() / ".last_collected"
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        return ""

    def _mark_last_collected(self, iso_ts: str) -> None:
        marker = self.cfg.sidechain_path() / ".last_collected"
        marker.write_text(iso_ts, encoding="utf-8")

    def _audit(self, action: str, **kwargs) -> None:
        """ sync_audit.jsonl"""
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
            print(f"[COLLECT] audit failed: {e}")
