"""
LedgerProvider — 经验账本（Append-only，供 EvoLoop 溯源）

设计：
- 每个操作自动记录到 sidechain/ledger.jsonl（append-only）
- 记录：timestamp, agent_id, action_type, result, error_sig, token_cost
- 用于 EvoLoop 的 ROI 计算：count(error) >= 3 && sum(token) > budget * 1.5
- 不做删除，只做追加（audit trail）
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
from ..runtime import MemoryProviderABC


class LedgerProvider(MemoryProviderABC):
    """经验账本提供者"""

    def __init__(self, ledger_path: str = "sidechain/ledger.jsonl"):
        self.ledger_path = Path(ledger_path)
        self.buffer = []  # 缓冲（会话期间）

    def initialize(self, context: dict) -> None:
        """启动时确保 ledger 目录存在"""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def prefetch(self, session_id: str) -> str:
        """返回近期错误统计"""
        if not self.ledger_path.exists():
            return "## Experience Ledger\n[No experience recorded yet]"

        try:
            lines = self.ledger_path.read_text().split("\n")[-20:]  # 最近 20 条
            entries = [json.loads(line) for line in lines if line.strip()]

            # 统计错误类型
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
    ) -> None:
        """记录一个操作到账本"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_id": agent_id,
            "action_type": action_type,
            "result": str(result)[:100],  # 截断长文本
            "error_sig": error_sig,
            "token_cost": token_cost,
        }
        self.buffer.append(entry)

    def sync_turn(self, action: dict, result: Any) -> None:
        """每轮同步 — 可选的轻量记录"""
        # 完整的记录应该通过 record() 显式调用
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """压缩前 — 账本不需要特殊保护"""
        pass

    def on_session_end(self) -> None:
        """会话结束 — 持久化所有缓冲的账本条目"""
        if not self.buffer:
            return

        try:
            # Append-only 写入
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                for entry in self.buffer:
                    f.write(json.dumps(entry) + "\n")
            print(f"[LEDGER] Recorded {len(self.buffer)} entries")
        except Exception as e:
            print(f"[WARN] Failed to write ledger: {e}")

    # 便利方法：EvoLoop 查询
    def count_error_occurrences(self, error_sig: str) -> int:
        """查询指定错误的发生次数"""
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
        """统计某错误类型的总 token 消耗"""
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
