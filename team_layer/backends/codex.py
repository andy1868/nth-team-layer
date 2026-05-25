"""
CodexBackend — OpenAI Codex CLI 适配器

OpenAI 的 codex CLI（基于 GPT-5），主要用于代码生成任务。
调用方式：subprocess + JSON 输出

适用场景：
- 用户已安装 `codex` CLI（OpenAI 官方）
- 需要专门的代码生成 Agent

实装：
- 通过 `codex --json --task "<task>"` 调用
- 解析 JSON 输出获取 code / explanation / usage
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Optional

from .base import (
    AgentBackend,
    BackendCapabilities,
    BackendUnavailableError,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    TurnResponse,
)


class CodexBackend(AgentBackend):
    """OpenAI Codex CLI 适配器"""

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

        # 拼装命令
        args = [self.cli_name, "--json"]
        if self.model:
            args += ["--model", self.model]
        if system_prompt:
            args += ["--system", system_prompt]

        env = {**os.environ, **self._session_config.env}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        cwd = str(self._session_config.workdir) if self._session_config.workdir else None

        try:
            proc = subprocess.run(
                args + ["--task", prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._session_config.timeout,
                env=env,
                cwd=cwd,
            )
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

        # 解析 JSON 输出
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            # 降级：把 stdout 当作纯文本响应
            payload = {"output": proc.stdout, "usage": {}}

        usage = TokenUsage(
            input_tokens=payload.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=payload.get("usage", {}).get("completion_tokens", 0),
        )
        latency = self._track_turn_end(start, usage)

        if proc.returncode != 0:
            return TurnResponse(
                content=payload.get("output", ""),
                finish_reason="error",
                usage=usage,
                latency_seconds=latency,
                error=f"codex exit {proc.returncode}: {(proc.stderr or '')[:300]}",
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
            supports_tools=False,  # Codex CLI 通常是 single-shot code gen
            supports_system_prompt=True,
            supports_multi_turn=False,
            max_context_tokens=64_000,
            notes="OpenAI Codex CLI. Best for one-shot code generation.",
        )
