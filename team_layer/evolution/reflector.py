"""
Reflector   Patch  Subagent

sidechain
PatchSKILL.md + Pydantic  +


- Subagent    Patch
- LLM    llm_callback  LLM
-  Z3   Pydantic Schema
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class Patch:
    """Reflector  Patch"""
    skill_id: str                          #  id
    error_sig: str                         #  patch
    description: str                       # <60 token
    trigger_pattern: str                   #
    risk_level: str                        # "low" / "medium" / "high"
    fix_steps: List[str]                   #
    contract: Dict[str, Dict[str, str]]    # Pydantic  {input: {field: type}, output: {...}}
    sample_failures: List[str] = field(default_factory=list)  #  patch
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    generator: str = "template"            # "template"  "llm"

    def to_skill_md(self) -> str:
        """ skills/registry/*.md """
        contract_yaml = "\n".join([
            f"  {kind}: {json.dumps(fields)}"
            for kind, fields in self.contract.items()
        ])
        steps = "\n".join([f"{i+1}. {s}" for i, s in enumerate(self.fix_steps)])

        return f"""id: {self.skill_id}
desc: "{self.description}"
trigger: "{self.trigger_pattern}"
risk: {self.risk_level}
error_sig: "{self.error_sig}"
generated_at: "{self.generated_at}"
generator: {self.generator}
contract:
{contract_yaml}

##
{steps}

##
{chr(10).join(['- ' + s[:80] for s in self.sample_failures[:3]])}
"""

    def to_dict(self) -> dict:
        return asdict(self)


class Reflector:
    """ Patch  Subagent"""

    #
    RISK_HEURISTICS = {
        "timeout": "low",
        "retry": "low",
        "rate_limit": "low",
        "connection_refused": "low",
        "lint": "low",
        "import": "low",
        "permission": "high",      #
        "auth": "high",
        "destructive": "high",
        "drop_table": "high",
        "rm_rf": "high",
    }

    def __init__(self, llm_callback: Optional[Callable] = None):
        """
        Args:
            llm_callback:  LLM  fn(prompt: str) -> str
                          :  JSON
                           None
        """
        self.llm_callback = llm_callback

    def reflect(
        self,
        error_sig: str,
        sample_logs: List[dict],
    ) -> Patch:
        """
         Patch

        Args:
            error_sig:  "timeout_database"
            sample_logs: ledger  entry

        Returns:
            Patch  skill +
        """
        sample_results = [str(log.get("result", ""))[:100] for log in sample_logs]

        if self.llm_callback:
            try:
                return self._llm_reflect(error_sig, sample_logs, sample_results)
            except Exception as e:
                print(f"[REFLECTOR] LLM failed ({e}), falling back to template")

        return self._template_reflect(error_sig, sample_results)

    def _template_reflect(self, error_sig: str, sample_results: List[str]) -> Patch:
        """ Patch LLM """
        risk = self._infer_risk(error_sig)
        skill_id = f"fix_{re.sub(r'[^a-z0-9_]', '_', error_sig.lower())}"

        #  error_sig
        fix_steps = self._template_fix_steps(error_sig)

        return Patch(
            skill_id=skill_id,
            error_sig=error_sig,
            description=f"Auto-generated fix for {error_sig}",
            trigger_pattern=re.escape(error_sig).replace(r"_", r".*"),
            risk_level=risk,
            fix_steps=fix_steps,
            contract={
                "input": {"code": "str", "error": "str"},
                "output": {"patched_code": "str", "applied": "bool"},
            },
            sample_failures=sample_results,
            generator="template",
        )

    def _llm_reflect(
        self,
        error_sig: str,
        sample_logs: List[dict],
        sample_results: List[str],
    ) -> Patch:
        """ LLM  Patch"""
        prompt = self._build_llm_prompt(error_sig, sample_logs)
        response = self.llm_callback(prompt)

        #  LLM  JSON
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                raise ValueError("LLM response is not valid JSON")
            data = json.loads(match.group(0))

        return Patch(
            skill_id=data.get("skill_id", f"fix_{error_sig.lower()}"),
            error_sig=error_sig,
            description=data.get("description", f"LLM fix for {error_sig}"),
            trigger_pattern=data.get("trigger_pattern", error_sig),
            risk_level=data.get("risk_level", self._infer_risk(error_sig)),
            fix_steps=data.get("fix_steps", []),
            contract=data.get("contract", {
                "input": {"code": "str"},
                "output": {"patched_code": "str"},
            }),
            sample_failures=sample_results,
            generator="llm",
        )

    def _infer_risk(self, error_sig: str) -> str:
        """"""
        sig_lower = error_sig.lower()
        for keyword, risk in self.RISK_HEURISTICS.items():
            if keyword in sig_lower:
                return risk
        return "medium"  #

    @staticmethod
    def _template_fix_steps(error_sig: str) -> List[str]:
        """ error_sig """
        sig_lower = error_sig.lower()

        if "timeout" in sig_lower or "connection" in sig_lower:
            return [
                "",
                " tenacity ",
                " @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1))",
                " timeout ",
            ]
        if "rate_limit" in sig_lower:
            return [
                " API ",
                "wait_exponential(min=1, max=60)",
                "",
            ]
        if "import" in sig_lower or "module" in sig_lower:
            return [
                " import  try/except ",
                " lazy import  importlib ",
                " requirements ",
            ]
        if "lint" in sig_lower:
            return [
                "ruff/black",
                " lint ",
            ]
        if "permission" in sig_lower or "auth" in sig_lower:
            return [
                "[HIGH RISK]  permission_gate ",
                " token/key ",
                " 7 ",
                "",
            ]

        return [
            f" {error_sig} ",
            "try/except + ",
            "",
        ]

    @staticmethod
    def _build_llm_prompt(error_sig: str, sample_logs: List[dict]) -> str:
        """ LLM """
        samples = "\n".join([
            f"  - {json.dumps(log)[:200]}"
            for log in sample_logs[:5]
        ])
        return f"""You are a Reflector subagent. Analyze recurring failure and generate a fix patch.

Error signature: {error_sig}
Sample failures from ledger:
{samples}

Generate a JSON patch with this exact schema:
{{
  "skill_id": "fix_<short_name>",
  "description": "<60-token description>",
  "trigger_pattern": "<regex matching this error>",
  "risk_level": "low" | "medium" | "high",
  "fix_steps": ["step1", "step2", ...],
  "contract": {{
    "input": {{"field": "type"}},
    "output": {{"field": "type"}}
  }}
}}

Return ONLY valid JSON, no preamble."""
