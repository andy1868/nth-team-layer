"""
EvoLoop — 自进化流水线编排器

串联三阶段：
    LedgerProvider → Trigger → Reflector → Verifier → Gate

提供两种入口：
    run_once()          — 单次扫描，触发所有满足 ROI 的进化
    run_for_sig(sig)    — 强制对指定签名进化（手动触发）
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .gate import EvolutionGate, GateDecision
from .reflector import Patch, Reflector
from .trigger import EvolutionDecision, EvoTrigger
from .verifier import VerifyResult, Verifier


@dataclass
class EvoCycleResult:
    """一个 EvoLoop 周期的完整结果（供主代理观测）"""
    decision: EvolutionDecision
    patch: Optional[Patch] = None
    verify: Optional[VerifyResult] = None
    gate: Optional[GateDecision] = None
    stopped_at: str = ""  # "trigger" / "reflector" / "verifier" / "gate" / "done"

    def summary(self) -> str:
        parts = [str(self.decision)]
        if self.patch:
            parts.append(f"  PATCH: {self.patch.skill_id} (risk={self.patch.risk_level})")
        if self.verify:
            parts.append(f"  {self.verify}")
        if self.gate:
            parts.append(f"  {self.gate}")
        parts.append(f"  STOPPED AT: {self.stopped_at}")
        return "\n".join(parts)


class EvoLoop:
    """EvoLoop 编排器"""

    def __init__(
        self,
        ledger,  # LedgerProvider
        trigger: Optional[EvoTrigger] = None,
        reflector: Optional[Reflector] = None,
        verifier: Optional[Verifier] = None,
        gate: Optional[EvolutionGate] = None,
        llm_callback: Optional[Callable] = None,
    ):
        """
        Args:
            ledger: LedgerProvider 实例（数据源）
            trigger/reflector/verifier/gate: 可选自定义实例，未提供则用默认配置
            llm_callback: 注入到 Reflector 的 LLM 回调
        """
        self.ledger = ledger
        self.trigger = trigger or EvoTrigger(ledger)
        self.reflector = reflector or Reflector(llm_callback=llm_callback)
        self.verifier = verifier or Verifier(use_docker=False)
        self.gate = gate or EvolutionGate()

    def run_once(self) -> List[EvoCycleResult]:
        """扫描账本，触发所有满足 ROI 的进化"""
        decisions = self.trigger.scan_all()
        if not decisions:
            print("[EVO] No error signatures meet evolution threshold.")
            return []

        print(f"[EVO] {len(decisions)} signature(s) triggered evolution.")
        results = []
        for decision in decisions:
            result = self._run_pipeline(decision)
            results.append(result)
        return results

    def run_for_sig(self, error_sig: str, force: bool = False) -> EvoCycleResult:
        """对单个错误签名运行 EvoLoop（force=True 跳过 trigger 门槛）"""
        decision = self.trigger.check(error_sig)
        if not decision.should_evolve and not force:
            return EvoCycleResult(decision=decision, stopped_at="trigger")

        if force and not decision.should_evolve:
            decision = EvolutionDecision(
                should_evolve=True,
                error_sig=error_sig,
                occurrences=decision.occurrences,
                wasted_tokens=decision.wasted_tokens,
                reason=f"FORCED (original: {decision.reason})",
            )

        return self._run_pipeline(decision)

    def _run_pipeline(self, decision: EvolutionDecision) -> EvoCycleResult:
        """完整三阶段流水线"""
        sig = decision.error_sig

        # Phase 1: 收集样本日志
        samples = self._collect_samples(sig, limit=5)
        if not samples:
            return EvoCycleResult(
                decision=decision,
                stopped_at="reflector",
            )

        # Phase 2: Reflector 生成 Patch
        try:
            patch = self.reflector.reflect(sig, samples)
        except Exception as e:
            print(f"[EVO] Reflector failed for {sig}: {e}")
            return EvoCycleResult(decision=decision, stopped_at="reflector")

        # Phase 3: Verifier 沙箱验证
        try:
            verify = self.verifier.verify(patch)
        except Exception as e:
            print(f"[EVO] Verifier crashed for {patch.skill_id}: {e}")
            return EvoCycleResult(decision=decision, patch=patch, stopped_at="verifier")

        # Phase 4: Gate 决策
        try:
            gate_decision = self.gate.decide(patch, verify)
        except Exception as e:
            print(f"[EVO] Gate failed for {patch.skill_id}: {e}")
            return EvoCycleResult(
                decision=decision, patch=patch, verify=verify, stopped_at="gate"
            )

        return EvoCycleResult(
            decision=decision,
            patch=patch,
            verify=verify,
            gate=gate_decision,
            stopped_at="done",
        )

    def _collect_samples(self, error_sig: str, limit: int = 5) -> List[dict]:
        """从账本收集匹配的样本日志（最近 N 条）"""
        ledger_path = Path(self.ledger.ledger_path)
        if not ledger_path.exists():
            return []

        matches = []
        try:
            for line in ledger_path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("error_sig") == error_sig:
                    matches.append(entry)
        except Exception as e:
            print(f"[EVO] Failed to collect samples: {e}")

        # 保留最近 N 条
        return matches[-limit:]
