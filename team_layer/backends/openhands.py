"""
OpenHandsBackend  All-Hands-AI/OpenHands

OpenHands OpenDevin  SWE Agent REST API
 endpoint: http://localhost:3000/api/conversations


1. POST /api/conversations
2. POST /api/conversations/<id>/send   prompt
3. GET  /api/conversations/<id>
4. DELETE /api/conversations/<id>

 send_turn  conversation
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


class OpenHandsBackend(AgentBackend):
    """OpenHands REST API """

    backend_id = "openhands"

    DEFAULT_URL = "http://localhost:3000"
    POLL_INTERVAL = 2

    def __init__(
        self,
        api_url: Optional[str] = None,
        token: Optional[str] = None,
        agent_class: str = "CodeActAgent",
        **kwargs,
    ):
        super().__init__(api_url=api_url, token=token, agent_class=agent_class, **kwargs)
        self.api_url = api_url or os.environ.get("OPENHANDS_API_URL", self.DEFAULT_URL)
        self.token = token or os.environ.get("OPENHANDS_TOKEN", "")
        self.agent_class = agent_class

    @classmethod
    def is_available(cls, api_url: Optional[str] = None, **kwargs) -> bool:
        """ OpenHands API """
        url = api_url or os.environ.get("OPENHANDS_API_URL", cls.DEFAULT_URL)
        try:
            req = urllib.request.Request(
                f"{url.rstrip('/')}/api/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start_session(self, config: SessionConfig) -> None:
        if not self.is_available(api_url=self.api_url):
            raise BackendUnavailableError(
                f"OpenHands not reachable at {self.api_url}. "
                "Start the server first or set OPENHANDS_API_URL."
            )
        self._session_config = config
        self._session_started_at = time.time()
        self._turn_count = 0
        self._cumulative_usage = TokenUsage()

    def send_turn(self, prompt: str, system_prompt: str = "") -> TurnResponse:
        if not self._session_config:
            raise RuntimeError("call start_session() first")

        start = self._track_turn_start()

        try:
            # 1.  conversation turn
            payload = {
                "agent_class": self.agent_class,
                "initial_user_msg": (
                    f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
                ),
            }
            conv_id = self._http_post("/api/conversations", payload).get("id")
            if not conv_id:
                raise RuntimeError("OpenHands did not return conversation id")

            # 2.
            deadline = start + self._session_config.timeout
            result = None
            while time.time() < deadline:
                status = self._http_get(f"/api/conversations/{conv_id}")
                if status.get("status") in ("FINISHED", "ERROR", "STOPPED"):
                    result = status
                    break
                time.sleep(self.POLL_INTERVAL)
            if not result:
                self._http_delete(f"/api/conversations/{conv_id}")
                latency = self._track_turn_end(start, TokenUsage())
                return TurnResponse(
                    content="",
                    finish_reason="timeout",
                    latency_seconds=latency,
                    error="conversation did not finish within timeout",
                )

            # 3.
            messages = result.get("messages", [])
            final_content = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    final_content = msg.get("content", "")
                    break

            usage = TokenUsage(
                input_tokens=result.get("usage", {}).get("input_tokens", 0),
                output_tokens=result.get("usage", {}).get("output_tokens", 0),
            )
            latency = self._track_turn_end(start, usage)

            #
            self._http_delete(f"/api/conversations/{conv_id}")

            return TurnResponse(
                content=final_content,
                finish_reason="stop" if result.get("status") == "FINISHED" else "error",
                usage=usage,
                latency_seconds=latency,
                metadata={"backend": self.backend_id, "conv_id": conv_id},
            )

        except Exception as e:
            latency = self._track_turn_end(start, TokenUsage())
            return TurnResponse(
                content="",
                finish_reason="error",
                latency_seconds=latency,
                error=f"{type(e).__name__}: {e}",
            )

    def end_session(self) -> SessionSummary:
        return self._build_summary()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,
            supports_tools=True,           # OpenHands
            supports_system_prompt=True,
            supports_multi_turn=True,
            max_context_tokens=200_000,
            notes=(
                "OpenHands (All-Hands-AI) via REST. "
                "Set OPENHANDS_API_URL (default: http://localhost:3000). "
                "Strong SWE-bench performance."
            ),
        )

    #   HTTP

    def _http_post(self, path: str, data: dict) -> dict:
        req = urllib.request.Request(
            f"{self.api_url.rstrip('/')}{path}",
            data=json.dumps(data).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.api_url.rstrip('/')}{path}",
            method="GET",
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_delete(self, path: str) -> None:
        try:
            req = urllib.request.Request(
                f"{self.api_url.rstrip('/')}{path}",
                method="DELETE",
                headers=self._headers(),
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass  #

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h
