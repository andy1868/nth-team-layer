"""
LedgerProvider  Append-only EvoLoop


-  sidechain/ledger.jsonlappend-only
- timestamp, agent_id, action_type, result, error_sig, token_cost
-  EvoLoop  ROI count(error) >= 3 && sum(token) > budget * 1.5
- audit trail
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
from ..runtime import MemoryProviderABC


class LedgerProvider(MemoryProviderABC):
    """"""

    def __init__(self, ledger_path: str = "sidechain/ledger.jsonl"):
        self.ledger_path = Path(ledger_path)
        self.buffer = []  #

    def initialize(self, context: dict) -> None:
        """ ledger """
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def prefetch(self, session_id: str) -> str:
        """"""
        if not self.ledger_path.exists():
            return "## Experience Ledger\n[No experience recorded yet]"

        try:
            lines = self.ledger_path.read_text().split("\n")[-20:]  #  20
            entries = [json.loads(line) for line in lines if line.strip()]

            #
            error_counts = {}
            for entry in entries:
                if "error_sig" in entry and entry["error_sig"]:
                    sig = entry["error_sig"]
                    error_counts[sig] = error_counts.get(sig, 0) + 1

            content = "## Recent Errors\n"
            for sig, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                content += f"- {sig}: {count} occurrences\n"

            return content
        except Exception as e:
            return f"## Experience Ledger\n[Error reading ledger: {e}]"

    def record(
        self,
        agent_id: str,
        action_type: str,
        result: Any,
        error_sig: Optional[str] = None,
        token_cost: int = 0,
        immediate_flush: bool = True,
    ) -> None:
        """


        Args:
            immediate_flush:  True   append
                             SIGTERM/
                            False  on_session_end
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_id": agent_id,
            "action_type": action_type,
            "result": str(result)[:100],
            "error_sig": error_sig,
            "token_cost": token_cost,
        }
        if immediate_flush:
            # append-only
            try:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.ledger_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
            except Exception as e:
                #  buffer  fallback
                self.buffer.append(entry)
                print(f"[LEDGER] immediate flush failed, buffered: {e}")
        else:
            self.buffer.append(entry)

    def sync_turn(self, action: dict, result: Any) -> None:
        """  """
        #  record()
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """  """
        pass

    def on_session_end(self) -> None:
        """  """
        if not self.buffer:
            return

        try:
            # Append-only
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                for entry in self.buffer:
                    f.write(json.dumps(entry) + "\n")
            print(f"[LEDGER] Recorded {len(self.buffer)} entries")
        except Exception as e:
            print(f"[WARN] Failed to write ledger: {e}")

    # EvoLoop
    def count_error_occurrences(self, error_sig: str) -> int:
        """"""
        if not self.ledger_path.exists():
            return 0

        count = 0
        try:
            for line in self.ledger_path.read_text().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("error_sig") == error_sig:
                    count += 1
        except Exception:
            pass

        return count

    def sum_token_cost_by_sig(self, error_sig: str) -> int:
        """ token """
        if not self.ledger_path.exists():
            return 0

        total = 0
        try:
            for line in self.ledger_path.read_text().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("error_sig") == error_sig:
                    total += entry.get("token_cost", 0)
        except Exception:
            pass

        return total
