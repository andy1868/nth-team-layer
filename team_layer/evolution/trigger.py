"""
EvoTrigger — ROI 滞后触发器

原则：
- 杜绝基于 LLM 的"未来预测"，只看历史硬指标
- 双重门槛：count >= 3 AND wasted_tokens > budget * 1.5
- 滞后机制：避免单次异常触发进化，必须有持续性失败模式

数据源：LedgerProvider 的 sidechain/ledger.jsonl
"""

import os
from dataclasses import dataclass
from typing import List, Dict, Optional


@dataclass
class EvolutionDecision:
    """触发器输出 — 是否进化 + 触发原因"""
    should_evolve: bool
    error_sig: Optional[str] = None
    occurrences: int = 0
    wasted_tokens: int = 0
    reason: str = ""

    def __str__(self) -> str:
        if self.should_evolve:
            return (
                f"EVOLVE [{self.error_sig}] "
                f"count={self.occurrences} wasted={self.wasted_tokens}t — {self.reason}"
            )
        return f"SKIP — {self.reason}"


class EvoTrigger:
    """ROI 滞后触发器"""

    # 默认门槛（可通过 env 覆盖）
    DEFAULT_MIN_OCCURRENCES = 3
    DEFAULT_BUDGET = 15000
    DEFAULT_WASTE_MULTIPLIER = 1.5

    def __init__(
        self,
        ledger,  # LedgerProvider 实例
        min_occurrences: Optional[int] = None,
        evolution_budget: Optional[int] = None,
        waste_multiplier: Optional[float] = None,
    ):
        """
        Args:
            ledger: LedgerProvider 实例
            min_occurrences: 最小发生次数（默认 3）
            evolution_budget: 进化预算 token（默认 15000，可由 EVOLUTION_BUDGET 环境变量覆盖）
            waste_multiplier: 浪费倍数（默认 1.5）
        """
        self.ledger = ledger
        self.min_occurrences = min_occurrences or self.DEFAULT_MIN_OCCURRENCES
        self.evolution_budget = evolution_budget or int(
            os.getenv("EVOLUTION_BUDGET", self.DEFAULT_BUDGET)
        )
        self.waste_multiplier = waste_multiplier or self.DEFAULT_WASTE_MULTIPLIER
        self.waste_threshold = self.evolution_budget * self.waste_multiplier

    def check(self, error_sig: str) -> EvolutionDecision:
        """检查单个错误签名是否触发进化"""
        count = self.ledger.count_error_occurrences(error_sig)
        wasted = self.ledger.sum_token_cost_by_sig(error_sig)

        # 双重门槛
        if count < self.min_occurrences:
            return EvolutionDecision(
                should_evolve=False,
                error_sig=error_sig,
                occurrences=count,
                wasted_tokens=wasted,
                reason=f"count={count} < threshold={self.min_occurrences}",
            )

        if wasted <= self.waste_threshold:
            return EvolutionDecision(
                should_evolve=False,
                error_sig=error_sig,
                occurrences=count,
                wasted_tokens=wasted,
                reason=f"wasted={wasted}t <= threshold={int(self.waste_threshold)}t",
            )

        return EvolutionDecision(
            should_evolve=True,
            error_sig=error_sig,
            occurrences=count,
            wasted_tokens=wasted,
            reason=f"ROI breach: count>={self.min_occurrences} AND wasted>{int(self.waste_threshold)}t",
        )

    def scan_all(self) -> List[EvolutionDecision]:
        """
        扫描账本中所有错误签名，返回触发了进化的决策列表

        用法：
            for decision in trigger.scan_all():
                if decision.should_evolve:
                    # 启动 Reflector
                    pass
        """
        sigs = self._collect_error_sigs()
        decisions = [self.check(sig) for sig in sigs]
        return [d for d in decisions if d.should_evolve]

    def _collect_error_sigs(self) -> List[str]:
        """从账本提取所有不同的 error_sig"""
        import json
        from pathlib import Path

        ledger_path = Path(self.ledger.ledger_path)
        if not ledger_path.exists():
            return []

        sigs = set()
        try:
            for line in ledger_path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                sig = entry.get("error_sig")
                if sig:
                    sigs.add(sig)
        except Exception as e:
            print(f"[TRIGGER] Failed to collect sigs: {e}")

        return sorted(sigs)
