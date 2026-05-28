"""
EvolutionGate   + Merge


- Low Risk (Lint/timeout/retry)   AUTO_MERGE  skills/registry/
- Medium Risk                     PENDING_REVIEW  .patch
- High Risk (auth/destructive)    REJECTED  ESCALATE

 append-only
- skills/registry/<skill_id>.md
- sidechain/pending_patches/<skill_id>.patch.json
- sidechain/evolution_audit.jsonl
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from .reflector import Patch
from .verifier import VerifyResult


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GateAction(str, Enum):
    AUTO_MERGE = "auto_merge"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


@dataclass
class GateDecision:
    """Gate """
    action: GateAction
    skill_id: str
    artifact_path: Optional[str] = None  #
    reason: str = ""

    def __str__(self) -> str:
        return f"GATE [{self.action.value.upper()}] {self.skill_id}  {self.artifact_path or 'n/a'} ({self.reason})"


class EvolutionGate:
    """   Patch """

    def __init__(
        self,
        skills_dir: str = "skills/registry",
        pending_dir: str = "sidechain/pending_patches",
        audit_log: str = "sidechain/evolution_audit.jsonl",
        auto_merge_risks: tuple = (RiskLevel.LOW,),
    ):
        """
        Args:
            skills_dir:
            pending_dir:  patch
            audit_log: Gate append-only
            auto_merge_risks:
        """
        self.skills_dir = Path(skills_dir)
        self.pending_dir = Path(pending_dir)
        self.audit_log = Path(audit_log)
        self.auto_merge_risks = set(auto_merge_risks)

        #
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)

    def decide(self, patch: Patch, verify_result: VerifyResult) -> GateDecision:
        """ Patch + """
        #
        if not verify_result.passed:
            decision = GateDecision(
                action=GateAction.REJECTED,
                skill_id=patch.skill_id,
                reason=f"Verifier failed: {verify_result.summary}",
            )
            self._audit(patch, verify_result, decision)
            return decision

        #
        try:
            risk = RiskLevel(patch.risk_level)
        except ValueError:
            risk = RiskLevel.MEDIUM  #

        if risk in self.auto_merge_risks:
            decision = self._auto_merge(patch)
        else:
            decision = self._pending_review(patch, risk)

        self._audit(patch, verify_result, decision)
        return decision

    def _auto_merge(self, patch: Patch) -> GateDecision:
        """ skills/registry/"""
        target = self.skills_dir / f"{patch.skill_id}.md"

        #  patch
        if target.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.skills_dir / f"{patch.skill_id}_{ts}.md"

        target.write_text(patch.to_skill_md(), encoding="utf-8")

        return GateDecision(
            action=GateAction.AUTO_MERGE,
            skill_id=patch.skill_id,
            artifact_path=str(target),
            reason=f"risk={patch.risk_level} (auto-mergeable)",
        )

    def _pending_review(self, patch: Patch, risk: RiskLevel) -> GateDecision:
        """ patch"""
        target = self.pending_dir / f"{patch.skill_id}.patch.json"

        #
        if target.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.pending_dir / f"{patch.skill_id}_{ts}.patch.json"

        payload = {
            "patch": patch.to_dict(),
            "rendered_skill_md": patch.to_skill_md(),
            "submitted_at": datetime.now().isoformat(),
            "risk_assessment": risk.value,
        }
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        return GateDecision(
            action=GateAction.PENDING_REVIEW,
            skill_id=patch.skill_id,
            artifact_path=str(target),
            reason=f"risk={risk.value} (requires human approval)",
        )

    def _audit(self, patch: Patch, verify: VerifyResult, decision: GateDecision) -> None:
        """append-only """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "skill_id": patch.skill_id,
            "error_sig": patch.error_sig,
            "risk_level": patch.risk_level,
            "generator": patch.generator,
            "verify_passed": verify.passed,
            "verify_summary": verify.summary,
            "action": decision.action.value,
            "artifact": decision.artifact_path,
            "reason": decision.reason,
        }
        try:
            with open(self.audit_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[GATE] Audit log failed: {e}")
