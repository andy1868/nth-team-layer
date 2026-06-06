"""
CodexBackend  OpenAI Codex CLI

OpenAI  codex CLI GPT-5
subprocess + JSON


-  `codex` CLIOpenAI
-  Agent


-  `codex --json --task "<task>"`
-  JSON  code / explanation / usage
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from .base import (
    AgentBackend,
    BackendCapabilities,
    BackendUnavailableError,
    PreflightResult,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    TurnResponse,
)


class CodexBackend(AgentBackend):
    """OpenAI Codex CLI """

    backend_id = "codex"

    def __init__(
        self,
        cli_name: str = "codex",
        model: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(cli_name=cli_name, model=model, **kwargs)
        self.cli_name = cli_name
        self.model = model

    @classmethod
    def is_available(cls, cli_name: str = "codex", **kwargs) -> bool:
        return shutil.which(cli_name) is not None

    def preflight_check(self, *, timeout: float = 5.0):
        """PR-1: real Codex exec round-trip.

        Doc-level failure mode #4: codex CLI is present but the
        underlying API key / network is broken; ``is_available()``
        returns True so attach proceeds, then ``send_turn`` hangs.
        Running a trivial ``codex exec "echo OK"`` under timeout
        surfaces that state before any work depends on it.
        """
        # G-9 (Voss audit): imports promoted to module scope.
        t0 = time.monotonic()
        if not shutil.which(self.cli_name):
            return PreflightResult(
                ok=False, backend_id=self.backend_id,
                checked_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                detail=f"codex CLI {self.cli_name!r} not in PATH",
            )
        try:
            result = subprocess.run(
                [self.cli_name, "exec", "echo OK"],
                capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return PreflightResult(
                ok=False, backend_id=self.backend_id,
                checked_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                detail=f"codex exec {type(exc).__name__}: {exc}",
            )
        ok = result.returncode == 0 and "OK" in result.stdout
        detail = "" if ok else (result.stderr or result.stdout).strip()[:200]
        return PreflightResult(
            ok=ok, backend_id=self.backend_id,
            checked_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            detail=detail,
            structured={"returncode": result.returncode},
        )

    def start_session(self, config: SessionConfig) -> None:
        if not self.is_available(cli_name=self.cli_name):
            raise BackendUnavailableError(
                f"Codex CLI '{self.cli_name}' not found in PATH. "
                "Install from OpenAI Codex distribution."
            )
        self._session_config = config
        self._session_started_at = time.time()
        self._turn_count = 0
        self._cumulative_usage = TokenUsage()

    def send_turn(self, prompt: str, system_prompt: str = "") -> TurnResponse:
        if not self._session_config:
            raise RuntimeError("call start_session() first")

        start = self._track_turn_start()

        #
        args = [self.cli_name, "--json"]
        if self.model:
            args += ["--model", self.model]
        if system_prompt:
            args += ["--system", system_prompt]

        env = {**os.environ, **self._session_config.env}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        cwd = str(self._session_config.workdir) if self._session_config.workdir else None

        try:
            #  Popen + binary read hermes.py  Python 3.14 Windows
            #  UTF-8 locale  subprocess.run reader thread  UnicodeDecodeError
            popen = subprocess.Popen(
                args + ["--task", prompt],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
            try:
                stdout_bytes = popen.stdout.read()
                stderr_bytes = popen.stderr.read()
                popen.wait(timeout=self._session_config.timeout)
            finally:
                try:
                    popen.stdout.close()
                    popen.stderr.close()
                except Exception:
                    pass
            returncode = popen.returncode
            stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
            stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="timeout",
                latency_seconds=latency,
                error=f"codex timed out after {self._session_config.timeout}s",
            )
        except Exception as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"{type(e).__name__}: {e}",
            )

        #  JSON
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            payload = {"output": stdout, "usage": {}}

        usage = TokenUsage(
            input_tokens=payload.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=payload.get("usage", {}).get("completion_tokens", 0),
        )
        latency = self._track_turn_end(start, usage)

        if returncode != 0:
            return TurnResponse(
                content=payload.get("output", ""),
                finish_reason="error",
                usage=usage,
                latency_seconds=latency,
                error=f"codex exit {returncode}: {stderr[:300]}",
            )

        return TurnResponse(
            content=payload.get("output", "") or payload.get("code", ""),
            finish_reason="stop",
            usage=usage,
            latency_seconds=latency,
            metadata={"backend": self.backend_id, **payload.get("metadata", {})},
        )

    def end_session(self) -> SessionSummary:
        return self._build_summary()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,
            supports_tools=False,  # Codex CLI  single-shot code gen
            supports_system_prompt=True,
            supports_multi_turn=False,
            max_context_tokens=64_000,
            notes="OpenAI Codex CLI. Best for one-shot code generation.",
        )
