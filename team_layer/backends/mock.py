"""
MockBackend   / demo  backend

100%
   "error"/"fail"   finish_reason="error"
   "tool"            mock tool_call
   "timeout"         timeout
                 "Mock response for: <prompt >"


-  / CI
- demo API key
- backend ABC
"""

import hashlib
import re
import time
from typing import Optional

from .base import (
    AgentBackend,
    BackendCapabilities,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    ToolCall,
    TurnResponse,
)


class MockBackend(AgentBackend):
    """ mock backend"""

    backend_id = "mock"

    def __init__(
        self,
        latency_ms: int = 0,
        fail_rate: float = 0.0,
        seed: int = 42,
        **kwargs,
    ):
        """
        Args:
            latency_ms:  turn
            fail_rate:  [0,1] prompt hash
            seed:
        """
        super().__init__(latency_ms=latency_ms, fail_rate=fail_rate, seed=seed, **kwargs)
        self.latency_ms = latency_ms
        self.fail_rate = fail_rate
        self.seed = seed

    @classmethod
    def is_available(cls, **kwargs) -> bool:
        """Mock """
        return True

    def start_session(self, config: SessionConfig) -> None:
        self._session_config = config
        self._session_started_at = time.time()
        self._turn_count = 0
        self._cumulative_usage = TokenUsage()

    def send_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> TurnResponse:
        start = self._track_turn_start()

        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

        #  token 4 chars/token
        input_tokens = (len(prompt) + len(system_prompt)) // 4

        #
        content, finish_reason, tool_calls, error = self._dispatch(prompt)
        output_tokens = len(content) // 4

        usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        latency = self._track_turn_end(start, usage)

        return TurnResponse(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
            latency_seconds=latency,
            error=error,
            metadata={
                "backend": self.backend_id,
                "turn_index": self._turn_count,
                "session_id": self._session_config.session_id if self._session_config else None,
            },
        )

    def end_session(self) -> SessionSummary:
        return self._build_summary(final_status="completed")

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_multi_turn=True,
            max_context_tokens=100_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
            notes="Deterministic template-based mock. Free + offline.",
        )

    #

    def _dispatch(self, prompt: str):
        """  (content, finish_reason, tool_calls, error)"""
        lower = prompt.lower()

        #
        if "fail" in lower or "raise error" in lower:
            return (
                "[mock error: prompt contained 'fail']",
                "error",
                [],
                "intentional failure (keyword 'fail' in prompt)",
            )

        #  timeout
        if "timeout" in lower:
            return (
                "[mock timeout: simulated]",
                "timeout",
                [],
                "intentional timeout",
            )

        #  tool call
        if "tool" in lower or "use_tool" in lower:
            tool_match = re.search(r"tool[:\s]+(\w+)", lower)
            tool_name = tool_match.group(1) if tool_match else "search_web"
            return (
                f"Calling tool: {tool_name}",
                "tool_call",
                [ToolCall(name=tool_name, arguments={"query": prompt[:50]}, id="mock-1")],
                None,
            )

        #  prompt hash
        if self.fail_rate > 0:
            h = int(hashlib.md5(prompt.encode("utf-8")).hexdigest(), 16) % 1000
            if h < self.fail_rate * 1000:
                return (
                    "[mock error: probabilistic fail]",
                    "error",
                    [],
                    f"random fail (rate={self.fail_rate})",
                )

        #
        summary = prompt[:60].replace("\n", " ")
        return (
            f"Mock response to: '{summary}{'...' if len(prompt) > 60 else ''}'",
            "stop",
            [],
            None,
        )
