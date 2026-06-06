"""External VC verifier hooks for v0.10 mandate proofs.

This module intentionally does not re-use NTH DAO's own verifier as a
fallback. F-9 is about cross-implementation evidence: a run is only a pass
when an external tool such as DIDKit or a vc-js wrapper actually verifies the
credential.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Union


DEFAULT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ExternalVCResult:
    verifier: str
    available: bool
    ok: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


Command = Union[str, Sequence[str]]


def _split_command(command: str) -> list[str]:
    """Parse a configured command string into argv without invoking a shell."""
    if os.name == "nt":
        # Windows paths use backslashes, so POSIX shlex would corrupt
        # unquoted paths such as C:\Tools\didkit.exe. posix=False preserves
        # backslashes but leaves surrounding quotes; strip those below.
        parts = shlex.split(command, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(command)


def _command_to_args(command: Command) -> list[str]:
    if isinstance(command, str):
        return _split_command(command.strip()) if command.strip() else []
    return [str(part) for part in command]


def _format_command(args: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    return shlex.join(list(args))


def _resolve_command(command: Optional[Command], env_name: str, default: str) -> list[str]:
    configured = command or os.environ.get(env_name) or default
    return _command_to_args(configured)


def _command_available(args: Sequence[str]) -> bool:
    exe = args[0] if args else ""
    return bool(exe and shutil.which(exe))


def verify_with_didkit(
    credential: Dict[str, Any],
    *,
    command: Optional[Command] = None,
    proof_purpose: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ExternalVCResult:
    """Verify a VC credential with the DIDKit CLI.

    The default command is ``didkit`` and can be overridden with the
    ``NTH_DAO_DIDKIT_COMMAND`` environment variable. DIDKit consumes the
    credential JSON on stdin.
    """
    cmd = _resolve_command(command, "NTH_DAO_DIDKIT_COMMAND", "didkit")
    command_text = _format_command(cmd)
    if not _command_available(cmd):
        return ExternalVCResult(
            verifier="didkit",
            available=False,
            ok=False,
            command=command_text,
            reason="didkit command not found",
        )

    purpose = (
        proof_purpose
        or credential.get("proof", {}).get("proofPurpose")
        or "assertionMethod"
    )
    args = [
        *cmd,
        "vc-verify-credential",
        "-p",
        str(purpose),
    ]
    payload = json.dumps(credential, ensure_ascii=False, separators=(",", ":"))
    try:
        completed = subprocess.run(
            args,
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ExternalVCResult(
            verifier="didkit",
            available=True,
            ok=False,
            command=_format_command(args),
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            reason=f"didkit timed out after {timeout_seconds}s",
        )
    return ExternalVCResult(
        verifier="didkit",
        available=True,
        ok=completed.returncode == 0,
        command=_format_command(args),
        stdout=completed.stdout,
        stderr=completed.stderr,
        reason="ok" if completed.returncode == 0 else "didkit returned non-zero",
    )


def verify_with_vcjs(
    credential: Dict[str, Any],
    *,
    command: Optional[Command] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ExternalVCResult:
    """Verify a VC credential with a caller-provided vc-js wrapper command.

    There is no universal vc-js CLI, so the command is intentionally explicit:
    set ``NTH_DAO_VCJS_COMMAND`` to a script that reads a credential JSON from
    stdin and exits 0 only when verification succeeds.
    """
    cmd = _resolve_command(command, "NTH_DAO_VCJS_COMMAND", "")
    command_text = _format_command(cmd)
    if not cmd:
        return ExternalVCResult(
            verifier="vc-js",
            available=False,
            ok=False,
            command="",
            reason="NTH_DAO_VCJS_COMMAND is not set",
        )
    if not _command_available(cmd):
        return ExternalVCResult(
            verifier="vc-js",
            available=False,
            ok=False,
            command=command_text,
            reason="vc-js command not found",
        )

    payload = json.dumps(credential, ensure_ascii=False, separators=(",", ":"))
    try:
        completed = subprocess.run(
            cmd,
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ExternalVCResult(
            verifier="vc-js",
            available=True,
            ok=False,
            command=command_text,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            reason=f"vc-js wrapper timed out after {timeout_seconds}s",
        )
    return ExternalVCResult(
        verifier="vc-js",
        available=True,
        ok=completed.returncode == 0,
        command=command_text,
        stdout=completed.stdout,
        stderr=completed.stderr,
        reason="ok" if completed.returncode == 0 else "vc-js returned non-zero",
    )
