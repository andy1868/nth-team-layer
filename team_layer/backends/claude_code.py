"""
ClaudeCodeBackend — 调用 Anthropic Claude Code CLI

策略：
- 调用 `claude -p "<prompt>" --output-format stream-json`
- 解析 stream-json 输出（NDJSON：一行一个 JSON 事件）
- 累计 token 用量，最后输出最终消息

支持环境：
- 用户已通过 npm 安装 @anthropic-ai/claude-code
- 或本地有 cli.js（用户在 NTH Agent Pro 里的 ClaudeCodeBridge 用过这种方式）

参考：
- 用户的 NTH Agent Pro: src/nth_agent_pro/claude_code_bridge.py 用类似机制
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional

from .base import (
    AgentBackend,
    BackendCapabilities,
    BackendUnavailableError,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    TurnResponse,
)


class ClaudeCodeBackend(AgentBackend):
    """Claude Code CLI 适配器"""

    backend_id = "claude_code"

    # CLI 入口探测顺序
    CLI_NAMES = ["claude", "claude-code"]

    def __init__(
        self,
        cli_path: Optional[str] = None,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        **kwargs,
    ):
        """
        Args:
            cli_path: 显式指定 CLI 路径（None 时自动探测）
            model: 模型名（如 "claude-sonnet-4-6"）
            allowed_tools: 允许的工具列表
        """
        super().__init__(cli_path=cli_path, model=model, **kwargs)
        self._cli_path = cli_path
        self.model = model
        self.allowed_tools = allowed_tools

    @classmethod
    def is_available(cls, cli_path: Optional[str] = None, **kwargs) -> bool:
        """探测 claude CLI 是否可用"""
        if cli_path:
            return Path(cli_path).exists()
        for name in cls.CLI_NAMES:
            if shutil.which(name):
                return True
        return False

    def _resolve_cli(self) -> str:
        """选择可用的 CLI 入口"""
        if self._cli_path:
            return self._cli_path
        for name in self.CLI_NAMES:
            found = shutil.which(name)
            if found:
                return found
        raise BackendUnavailableError(
            "Claude Code CLI not found. Install with: "
            "npm install -g @anthropic-ai/claude-code"
        )

    def start_session(self, config: SessionConfig) -> None:
        if not self.is_available(cli_path=self._cli_path):
            raise BackendUnavailableError("claude CLI not available")
        self._session_config = config
        self._session_started_at = time.time()
        self._turn_count = 0
        self._cumulative_usage = TokenUsage()

    def send_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> TurnResponse:
        if not self._session_config:
            raise RuntimeError("call start_session() first")

        start = self._track_turn_start()
        cli = self._resolve_cli()

        # 组装命令（参考 ClaudeCodeBridge 的用法）
        args = [cli, "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            args += ["--model", self.model]
        if self.allowed_tools:
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if system_prompt:
            args += ["--append-system-prompt", system_prompt]

        # workdir
        cwd = str(self._session_config.workdir) if self._session_config.workdir else None
        env = {**os.environ, **self._session_config.env}
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            # 用 Popen + binary read（参见 hermes.py 注释：避免 Python 3.14 Windows
            # 非 UTF-8 locale 下 subprocess.run reader thread 的 UnicodeDecodeError）
            popen = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                stdout_bytes, stderr_bytes = popen.communicate(
                    input=prompt.encode("utf-8"),
                    timeout=self._session_config.timeout,
                )
            finally:
                # communicate 已经关闭了 PIPE，这里只是 defensive
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
                error=f"claude CLI timed out after {self._session_config.timeout}s",
            )
        except Exception as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"subprocess failed: {type(e).__name__}: {e}",
            )

        # 解析 stream-json (NDJSON)
        content, usage, finish_reason, error = self._parse_stream_json(
            stdout, stderr, returncode
        )
        latency = self._track_turn_end(start, usage)

        return TurnResponse(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            latency_seconds=latency,
            error=error,
            metadata={"backend": self.backend_id, "cli": cli},
        )

    def end_session(self) -> SessionSummary:
        return self._build_summary()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_multi_turn=False,  # 当前实现是一次性
            max_context_tokens=200_000,
            cost_per_1k_input=3.0,    # Sonnet pricing ~ $3/M
            cost_per_1k_output=15.0,
            notes=(
                "Anthropic Claude Code CLI. "
                "Install: npm install -g @anthropic-ai/claude-code. "
                "Requires ANTHROPIC_API_KEY or claude login."
            ),
        )

    # ─── 内部 ───

    def _parse_stream_json(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> tuple:
        """
        解析 Claude Code 的 stream-json 输出（NDJSON）

        返回：(content, usage, finish_reason, error)
        """
        if returncode != 0:
            error = (stderr or "")[:500] or f"exit {returncode}"
            return "", TokenUsage(), "error", error

        content_parts = []
        usage = TokenUsage()
        finish_reason = "stop"

        for line in (stdout or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # 文本内容
            if event_type == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        content_parts.append(block.get("text", ""))

            # 最终结果（含 usage）
            elif event_type == "result":
                if event.get("subtype") == "success":
                    finish_reason = "stop"
                else:
                    finish_reason = event.get("subtype", "error")
                result_usage = event.get("usage", {})
                usage.input_tokens = result_usage.get("input_tokens", 0)
                usage.output_tokens = result_usage.get("output_tokens", 0)

        return "\n".join(content_parts).strip(), usage, finish_reason, None
