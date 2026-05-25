"""
HermesBackend — 调用 hermes-agent CLI

策略：
1. 探测 hermes 包是否安装（python -c "import hermes")
2. 通过 subprocess 调用 `python -m hermes.cli`（或 `hermes` 可执行入口）
3. 用 batch_runner 风格的一次性 prompt → response 模式

适用场景：
- 用户已 `pip install -e .` 当前 Hermes 仓库
- 想用 Hermes 的强大 tool/skill 体系，同时享受 Team Layer 的协作能力

如果 Hermes 未安装，is_available() 返回 False，整个 backend 优雅降级。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


class HermesBackend(AgentBackend):
    """Hermes Agent 适配器（subprocess 模式）"""

    backend_id = "hermes"

    # 探测 Hermes 安装位置（按顺序尝试）
    HERMES_ENTRYPOINTS = [
        ["hermes"],                       # 安装到 PATH 的可执行
        [sys.executable, "-m", "hermes"], # python -m hermes
    ]

    def __init__(
        self,
        model: Optional[str] = None,
        hermes_args: Optional[list] = None,
        **kwargs,
    ):
        """
        Args:
            model: 模型名（如 "anthropic/claude-sonnet-4-6"）
            hermes_args: 额外传给 hermes CLI 的参数
        """
        super().__init__(model=model, **kwargs)
        self.model = model
        self.hermes_args = hermes_args or []
        self._cmd: Optional[list] = None  # 缓存可用入口

    @classmethod
    def is_available(cls, **kwargs) -> bool:
        """探测 hermes CLI 是否可用"""
        # 1. 试 PATH 中的 hermes 可执行
        if shutil.which("hermes"):
            return True
        # 2. 试 python -m hermes
        try:
            result = subprocess.run(
                [sys.executable, "-c", "import hermes; print('ok')"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and "ok" in (result.stdout or "")
        except Exception:
            return False

    def _resolve_cmd(self) -> list:
        """选择可用的 hermes 入口"""
        if self._cmd is not None:
            return self._cmd
        for cmd in self.HERMES_ENTRYPOINTS:
            try:
                result = subprocess.run(
                    cmd + ["--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    self._cmd = cmd
                    return cmd
            except Exception:
                continue
        raise BackendUnavailableError("no working hermes entrypoint found")

    def start_session(self, config: SessionConfig) -> None:
        if not self.is_available():
            raise BackendUnavailableError(
                "HermesBackend requires `pip install -e .` (Hermes is in this repo)"
            )
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
        cmd = self._resolve_cmd()

        # 拼接 hermes 调用参数（batch / one-shot 模式）
        full_args = list(cmd)
        full_args += ["--non-interactive"]
        if self.model:
            full_args += ["--model", self.model]
        if system_prompt:
            full_args += ["--system", system_prompt]
        if self._session_config.workdir:
            full_args += ["--cwd", str(self._session_config.workdir)]
        full_args += self.hermes_args
        full_args += ["--prompt", prompt]

        env = {**os.environ, **self._session_config.env}
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            proc = subprocess.run(
                full_args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._session_config.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="timeout",
                latency_seconds=latency,
                error=f"hermes timed out after {self._session_config.timeout}s",
            )
        except Exception as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"subprocess failed: {type(e).__name__}: {e}",
            )

        # 解析输出
        content = (proc.stdout or "").strip()
        usage = self._extract_usage(proc.stdout, proc.stderr)
        latency = self._track_turn_end(start, usage)

        if proc.returncode != 0:
            return TurnResponse(
                content=content,
                finish_reason="error",
                usage=usage,
                latency_seconds=latency,
                error=f"hermes exit {proc.returncode}: {(proc.stderr or '')[:300]}",
            )

        return TurnResponse(
            content=content,
            finish_reason="stop",
            usage=usage,
            latency_seconds=latency,
            metadata={"backend": self.backend_id, "cmd": cmd},
        )

    def end_session(self) -> SessionSummary:
        return self._build_summary()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,  # subprocess 模式默认不流式
            supports_tools=True,        # Hermes 有强大的 tool/skill 体系
            supports_system_prompt=True,
            supports_multi_turn=False,  # 当前实现是 batch one-shot
            max_context_tokens=200_000,
            notes="Hermes via subprocess. Install with: pip install -e . (current repo)",
        )

    # ─── 内部辅助 ───

    @staticmethod
    def _extract_usage(stdout: str, stderr: str) -> TokenUsage:
        """从 Hermes 输出解析 token 用量（best-effort）"""
        usage = TokenUsage()
        # Hermes 的 statusline 通常带 token 信息：寻找类似 "[12345 tokens]"
        combined = (stdout or "") + "\n" + (stderr or "")
        import re
        m = re.search(r"input[:\s]+(\d+)\s*tokens?", combined, re.I)
        if m:
            usage.input_tokens = int(m.group(1))
        m = re.search(r"output[:\s]+(\d+)\s*tokens?", combined, re.I)
        if m:
            usage.output_tokens = int(m.group(1))
        return usage
