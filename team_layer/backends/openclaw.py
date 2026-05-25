"""
OpenClawBackend — OpenClaw 适配器（stub）

OpenClaw 是 Claude Code 的开源对标（持久化 Runtime + 自治 24/7）。
通信协议：ACP (Agent Conversation Protocol) — HTTP/JSON.

当前为 stub 实现：
- is_available() 默认返回 False（除非 OPENCLAW_API_URL 已设置）
- send_turn() 通过 HTTP POST 到 ACP endpoint
- 用户提供 OPENCLAW_API_URL + OPENCLAW_TOKEN 后即可使用

实装指南：
1. 用户在 OpenClaw 服务端启动 ACP listener
2. 设置 env：OPENCLAW_API_URL=http://localhost:8080
3. 实例化时传入 token 或 env：OPENCLAW_TOKEN=xxx
4. 后续可以扩展为长连接（webhook / SSE）
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
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


class OpenClawBackend(AgentBackend):
    """OpenClaw ACP HTTP 适配器"""

    backend_id = "openclaw"

    DEFAULT_TIMEOUT = 60

    def __init__(
        self,
        api_url: Optional[str] = None,
        token: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(api_url=api_url, token=token, model=model, **kwargs)
        self.api_url = api_url or os.environ.get("OPENCLAW_API_URL")
        self.token = token or os.environ.get("OPENCLAW_TOKEN", "")
        self.model = model

    @classmethod
    def is_available(cls, api_url: Optional[str] = None, **kwargs) -> bool:
        """检测：env 或参数中是否提供了 API URL"""
        url = api_url or os.environ.get("OPENCLAW_API_URL")
        return bool(url)

    def start_session(self, config: SessionConfig) -> None:
        if not self.api_url:
            raise BackendUnavailableError(
                "OpenClawBackend requires OPENCLAW_API_URL env var "
                "(or api_url kwarg). See team_layer/backends/openclaw.py for setup."
            )
        self._session_config = config
        self._session_started_at = time.time()
        self._turn_count = 0
        self._cumulative_usage = TokenUsage()

    def send_turn(self, prompt: str, system_prompt: str = "") -> TurnResponse:
        if not self._session_config:
            raise RuntimeError("call start_session() first")

        start = self._track_turn_start()

        payload = {
            "session_id": self._session_config.session_id,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "model": self.model,
            "max_tokens": self._session_config.max_tokens,
            "temperature": self._session_config.temperature,
            "metadata": {"backend": self.backend_id},
        }

        try:
            req = urllib.request.Request(
                f"{self.api_url.rstrip('/')}/acp/turn",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}" if self.token else "",
                    "User-Agent": "team-layer-agent/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=self._session_config.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"HTTP {e.code}: {e.reason}",
            )
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"{type(e).__name__}: {e}",
            )
        except Exception as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"unexpected: {type(e).__name__}: {e}",
            )

        usage = TokenUsage(
            input_tokens=body.get("usage", {}).get("input_tokens", 0),
            output_tokens=body.get("usage", {}).get("output_tokens", 0),
        )
        latency = self._track_turn_end(start, usage)

        return TurnResponse(
            content=body.get("content", ""),
            finish_reason=body.get("finish_reason", "stop"),
            usage=usage,
            latency_seconds=latency,
            metadata={"backend": self.backend_id, **body.get("metadata", {})},
        )

    def end_session(self) -> SessionSummary:
        return self._build_summary()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,
            supports_tools=True,
            supports_system_prompt=True,
            supports_multi_turn=True,  # ACP 原生支持
            max_context_tokens=200_000,
            notes=(
                "OpenClaw via ACP HTTP. "
                "Set OPENCLAW_API_URL env var to enable. "
                "Persistent runtime, 24/7 autonomous mode."
            ),
        )
