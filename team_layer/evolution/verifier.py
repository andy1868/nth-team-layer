"""
Verifier   Subagent


-
    1.   Patch
    2.   subprocess + Docker
- Surrogate  Opaque Pass/Fail +  Patch
-  Pydantic Schema
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .reflector import Patch


@dataclass
class VerifyResult:
    """ Opaque Pass/Fail"""
    passed: bool
    skill_id: str
    summary: str = ""
    static_errors: List[str] = field(default_factory=list)
    runtime_errors: List[str] = field(default_factory=list)
    used_docker: bool = False

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"VERIFY [{status}] {self.skill_id}  {self.summary}"


class Verifier:
    """"""

    REQUIRED_FIELDS = ["skill_id", "error_sig", "description", "trigger_pattern", "risk_level", "fix_steps", "contract"]
    VALID_RISK_LEVELS = {"low", "medium", "high"}
    VALID_TYPES = {"str", "int", "float", "bool", "list", "dict", "Any"}

    def __init__(self, use_docker: bool = False, sandbox_timeout: int = 10):
        """
        Args:
            use_docker:  Docker  docker  subprocess
            sandbox_timeout:
        """
        self.use_docker = use_docker and self._docker_available()
        self.sandbox_timeout = sandbox_timeout

    def verify(self, patch: Patch) -> VerifyResult:
        """ Patch """
        #
        static_errors = self._static_check(patch)
        if static_errors:
            return VerifyResult(
                passed=False,
                skill_id=patch.skill_id,
                summary=f"Static check failed ({len(static_errors)} errors)",
                static_errors=static_errors,
                used_docker=False,
            )

        #
        runtime_errors = self._sandbox_check(patch)
        if runtime_errors:
            return VerifyResult(
                passed=False,
                skill_id=patch.skill_id,
                summary=f"Sandbox check failed ({len(runtime_errors)} errors)",
                runtime_errors=runtime_errors,
                used_docker=self.use_docker,
            )

        return VerifyResult(
            passed=True,
            skill_id=patch.skill_id,
            summary=f"All checks passed (risk={patch.risk_level})",
            used_docker=self.use_docker,
        )

    def _static_check(self, patch: Patch) -> List[str]:
        """Patch  + """
        errors = []

        #
        patch_dict = patch.to_dict()
        for field_name in self.REQUIRED_FIELDS:
            if not patch_dict.get(field_name):
                errors.append(f"Missing required field: {field_name}")

        #
        if patch.risk_level not in self.VALID_RISK_LEVELS:
            errors.append(f"Invalid risk_level: {patch.risk_level} (expected {self.VALID_RISK_LEVELS})")

        #
        if not patch.fix_steps:
            errors.append("fix_steps cannot be empty")

        #
        if not isinstance(patch.contract, dict):
            errors.append("contract must be a dict")
        else:
            if "input" not in patch.contract or "output" not in patch.contract:
                errors.append("contract must contain 'input' and 'output' keys")
            else:
                #
                for kind in ("input", "output"):
                    fields = patch.contract.get(kind, {})
                    if not isinstance(fields, dict):
                        errors.append(f"contract.{kind} must be a dict")
                        continue
                    for fname, ftype in fields.items():
                        if ftype not in self.VALID_TYPES:
                            errors.append(
                                f"contract.{kind}.{fname} has invalid type '{ftype}'"
                            )

        # trigger_pattern
        if patch.trigger_pattern:
            import re
            try:
                re.compile(patch.trigger_pattern)
            except re.error as e:
                errors.append(f"Invalid regex in trigger_pattern: {e}")

        return errors

    def _sandbox_check(self, patch: Patch) -> List[str]:
        """"""
        errors = []

        with tempfile.TemporaryDirectory(prefix="evoloop_") as tmpdir:
            tmp_path = Path(tmpdir)

            #
            script_path = tmp_path / "verify_contract.py"
            script_path.write_text(
                self._build_verify_script(patch),
                encoding="utf-8",
            )

            #
            try:
                if self.use_docker:
                    result = self._run_in_docker(tmp_path, script_path)
                else:
                    result = self._run_in_subprocess(script_path)

                if result["returncode"] != 0:
                    errors.append(f"Sandbox exit code {result['returncode']}: {result['stderr'][:200]}")
                else:
                    #  JSON
                    try:
                        verify_output = json.loads(result["stdout"])
                        if not verify_output.get("ok"):
                            errors.append(f"Contract verification failed: {verify_output.get('error')}")
                    except json.JSONDecodeError:
                        errors.append(f"Sandbox produced non-JSON output: {result['stdout'][:200]}")
            except subprocess.TimeoutExpired:
                errors.append(f"Sandbox timed out (>{self.sandbox_timeout}s)")
            except Exception as e:
                errors.append(f"Sandbox launch failed: {type(e).__name__}: {e}")

        return errors

    def _run_in_subprocess(self, script_path: Path) -> Dict:
        """subprocess  Docker """
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=self.sandbox_timeout,
            #
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    def _run_in_docker(self, workdir: Path, script_path: Path) -> Dict:
        """Docker """
        rel_script = script_path.relative_to(workdir).as_posix()
        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", "none",
                "--memory", "256m",
                "--cpus", "0.5",
                "-v", f"{workdir}:/work:ro",
                "-w", "/work",
                "python:3.11-slim",
                "python", rel_script,
            ],
            capture_output=True,
            text=True,
            timeout=self.sandbox_timeout + 5,  # docker  overhead
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @staticmethod
    def _docker_available() -> bool:
        return shutil.which("docker") is not None

    @staticmethod
    def _build_verify_script(patch: Patch) -> str:
        """ Z3 Pydantic """
        contract_json = json.dumps(patch.contract)
        return f'''"""  """
import json
import sys

CONTRACT = {contract_json}

TYPE_MAP = {{
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}}


def make_sample(type_name):
    """"""
    samples = {{
        "str": "sample",
        "int": 42,
        "float": 3.14,
        "bool": True,
        "list": [],
        "dict": {{}},
        "Any": None,
    }}
    return samples.get(type_name)


def check_kind(kind, fields):
    """input/output"""
    instance = {{name: make_sample(t) for name, t in fields.items()}}
    for name, t in fields.items():
        py_type = TYPE_MAP.get(t)
        if py_type is None:
            # "Any"
            continue
        if not isinstance(instance[name], py_type):
            return f"{{kind}}.{{name}} expected {{t}}, got {{type(instance[name]).__name__}}"
    return None


def main():
    try:
        #  pydantic isinstance
        try:
            from pydantic import BaseModel, create_model
            for kind in ("input", "output"):
                fields = CONTRACT.get(kind, {{}})
                model_fields = {{
                    name: (TYPE_MAP.get(t, object), ...)
                    for name, t in fields.items()
                }}
                if model_fields:
                    Model = create_model(f"Contract_{{kind}}", **model_fields)
                    sample = {{name: make_sample(t) for name, t in fields.items()}}
                    Model(**sample)
        except ImportError:
            #
            for kind in ("input", "output"):
                err = check_kind(kind, CONTRACT.get(kind, {{}}))
                if err:
                    print(json.dumps({{"ok": False, "error": err}}))
                    sys.exit(0)

        print(json.dumps({{"ok": True}}))
    except Exception as e:
        print(json.dumps({{"ok": False, "error": f"{{type(e).__name__}}: {{e}}"}}))


if __name__ == "__main__":
    main()
'''
