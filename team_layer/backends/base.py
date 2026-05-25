"""
AgentBackend ABC + 数据类

所有 backend 必须实现：
    is_available()       — 探测 backend 是否可用（无副作用）
    start_session()      — 启动一次对话
    send_turn()          — 同步发送一轮，返回响应
    end_session()        — 收尾，返回摘要

可选实现：
    stream_turn()        — 流式响应（默认包装 send_turn 为单 chunk）
    capabilities()       — 自描述能力（默认基础值）
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# ─────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────

class BackendUnavailableError(RuntimeError):
    """backend 检测到自己不可用（未安装、API key 缺失等）"""


# ─────────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────────

@dataclass
class TokenUsage:
    """单次 / 累计 token 用量"""
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
    """工具调用请求（backend 可在 turn 内返回多个）"""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class SessionConfig:
    """启动一次 backend 会话的参数"""
    session_id: str
    goal: str
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 120
    workdir: Optional[Path] = None
    env: Dict[str, str] = field(default_factory=dict)
    # 工具相关
    allowed_tools: Optional[List[str]] = None  # None = backend 默认
    # 额外参数（透传给具体 backend）
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResponse:
    """单轮响应"""
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
    """会话结束摘要"""
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
    """backend 自描述（用于 Team Layer 决策）"""
    supports_streaming: bool = False
    supports_tools: bool = False
    supports_system_prompt: bool = True
    supports_multi_turn: bool = True
    max_context_tokens: int = 8192
    # 成本估算（USD / 1k tokens）— 0 表示未知
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    notes: str = ""


# ─────────────────────────────────────────────────────────────
# AgentBackend ABC
# ─────────────────────────────────────────────────────────────

class AgentBackend(abc.ABC):
    """所有 Agent 框架适配器的基类"""

    #: 唯一标识，子类必须覆盖（"hermes" / "claude_code" / ...）
    backend_id: str = "abstract"

    def __init__(self, **kwargs):
        """子类可接收 backend 特定的配置"""
        self.config = kwargs
        self._session_config: Optional[SessionConfig] = None
        self._session_started_at: float = 0.0
        self._turn_count: int = 0
        self._cumulative_usage: TokenUsage = TokenUsage()

    # ─── 必须实现 ───

    @classmethod
    @abc.abstractmethod
    def is_available(cls, **kwargs) -> bool:
        """检查 backend 是否可用（执行环境探测，但不产生副作用）"""

    @abc.abstractmethod
    def start_session(self, config: SessionConfig) -> None:
        """启动一次会话"""

    @abc.abstractmethod
    def send_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> TurnResponse:
        """同步发送一轮"""

    @abc.abstractmethod
    def end_session(self) -> SessionSummary:
        """结束会话并返回摘要"""

    # ─── 可选实现 ───

    def stream_turn(
        self,
        prompt: str,
        system_prompt: str = "",
    ) -> Iterator[str]:
        """
        流式发送（默认实现：调 send_turn 后包成单 chunk）

        子类如果原生支持流式，可覆写此方法
        """
        response = self.send_turn(prompt, system_prompt)
        yield response.content

    def capabilities(self) -> BackendCapabilities:
        """声明 backend 能力（默认为基础值）"""
        return BackendCapabilities()

    def cancel(self) -> None:
        """中断正在进行的 turn（如果 backend 支持）"""
        pass

    # ─── 内部辅助 ───

    def _track_turn_start(self) -> float:
        """子类在 send_turn 起始调用，返回开始时间"""
        self._turn_count += 1
        return time.time()

    def _track_turn_end(self, start_at: float, usage: TokenUsage) -> float:
        """子类在 send_turn 结束调用，累计用量，返回延迟"""
        self._cumulative_usage = self._cumulative_usage + usage
        return time.time() - start_at

    def _build_summary(
        self,
        final_status: str = "completed",
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SessionSummary:
        """生成 SessionSummary（子类在 end_session 中调用）"""
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
