"""
AgentBackend ABC +

 backend
    is_available()         backend
    start_session()
    send_turn()
    end_session()


    stream_turn()          send_turn  chunk
    capabilities()
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


#
# Exceptions
#

class BackendUnavailableError(RuntimeError):
    """backend API key """


#
#
#

@dataclass
class TokenUsage:
    """ /  token """
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class ToolCall:
    """backend  turn """
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class SessionConfig:
    """ backend """
    session_id: str
    goal: str
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 120
    workdir: Optional[Path] = None
    env: Dict[str, str] = field(default_factory=dict)
    #
    allowed_tools: Optional[List[str]] = None  # None = backend
    #  backend
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResponse:
    """"""
    content: str
    finish_reason: str = "stop"   # stop / length / tool_call / error / timeout
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: List[ToolCall] = field(default_factory=list)
    latency_seconds: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.finish_reason == "error" or self.error is not None


@dataclass
class SessionSummary:
    """"""
    session_id: str
    backend_id: str
    total_turns: int
    total_usage: TokenUsage
    duration_seconds: float
    final_status: str = "completed"  # completed / interrupted / error / timeout
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendCapabilities:
    """backend  NTH DAO runtime """
    supports_streaming: bool = False
    supports_tools: bool = False
    supports_system_prompt: bool = True
    supports_multi_turn: bool = True
    max_context_tokens: int = 8192
    # USD / 1k tokens 0
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    notes: str = ""


#
# AgentBackend ABC
#

class AgentBackend(abc.ABC):
    """ Agent """

    #: "hermes" / "claude_code" / ...
    backend_id: str = "abstract"

    def __init__(self, **kwargs):
        """ backend """
        self.config = kwargs
        self._session_config: Optional[SessionConfig] = None
        self._session_started_at: float = 0.0
        self._turn_count: int = 0
        self._cumulative_usage: TokenUsage = TokenUsage()

    #

    @classmethod
    @abc.abstractmethod
    def is_available(cls, **kwargs) -> bool:
        """ backend """

    @abc.abstractmethod
    def start_session(self, config: SessionConfig) -> None:
        """"""

    @abc.abstractmethod
    def send_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> TurnResponse:
        """"""

    @abc.abstractmethod
    def end_session(self) -> SessionSummary:
        """"""

    #

    def stream_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> Iterator[str]:
        """
         send_turn  chunk


        """
        response = self.send_turn(prompt, system_prompt)
        yield response.content

    def capabilities(self) -> BackendCapabilities:
        """ backend """
        return BackendCapabilities()

    def cancel(self) -> None:
        """ turn backend """
        pass

    #

    def _track_turn_start(self) -> float:
        """ send_turn """
        self._turn_count += 1
        return time.time()

    def _track_turn_end(self, start_at: float, usage: TokenUsage) -> float:
        """ send_turn """
        self._cumulative_usage = self._cumulative_usage + usage
        return time.time() - start_at

    def _build_summary(
        self,
        final_status: str = "completed",
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionSummary:
        """ SessionSummary end_session """
        duration = time.time() - self._session_started_at if self._session_started_at else 0.0
        session_id = self._session_config.session_id if self._session_config else "unknown"
        return SessionSummary(
            session_id=session_id,
            backend_id=self.backend_id,
            total_turns=self._turn_count,
            total_usage=self._cumulative_usage,
            duration_seconds=duration,
            final_status=final_status,
            error=error,
            metadata=metadata or {},
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} backend_id={self.backend_id!r}>"
