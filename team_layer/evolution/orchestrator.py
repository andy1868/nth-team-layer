"""
EvoLoop


    LedgerProvider  Trigger  Reflector  Verifier  Gate


    run_once()            ROI
    run_for_sig(sig)
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
    """ EvoLoop """
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
    """EvoLoop """

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
            ledger: LedgerProvider
            trigger/reflector/verifier/gate:
            llm_callback:  Reflector  LLM
        """
        self.ledger = ledger
        self.trigger = trigger or EvoTrigger(ledger)
        self.reflector = reflector or Reflector(llm_callback=llm_callback)
        self.verifier = verifier or Verifier(use_docker=False)
        self.gate = gate or EvolutionGate()

    def run_once(self) -> List[EvoCycleResult]:
        """ ROI """
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
        """ EvoLoopforce=True  trigger """
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
        """"""
        sig = decision.error_sig

        # Phase 1:
        samples = self._collect_samples(sig, limit=5)
        if not samples:
            return EvoCycleResult(
                decision=decision,
                stopped_at="reflector",
            )

        # Phase 2: Reflector  Patch
        try:
            patch = self.reflector.reflect(sig, samples)
        except Exception as e:
            print(f"[EVO] Reflector failed for {sig}: {e}")
            return EvoCycleResult(decision=decision, stopped_at="reflector")

        # Phase 3: Verifier
        try:
            verify = self.verifier.verify(patch)
        except Exception as e:
            print(f"[EVO] Verifier crashed for {patch.skill_id}: {e}")
            return EvoCycleResult(decision=decision, patch=patch, stopped_at="verifier")

        # Phase 4: Gate
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
        """ N """
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

        #  N
        return matches[-limit:]
