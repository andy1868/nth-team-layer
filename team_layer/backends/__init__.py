"""
team_layer.backends — Agent 框架适配器层

把"Agent 决策核心"做成可替换的 backend，让同一个 Team Layer（记忆/压缩/EvoLoop/git_sync/Blackboard）
能为多种 Agent 框架提供服务：

    HermesBackend         hermes-agent (NousResearch)
    ClaudeCodeBackend     Anthropic Claude Code CLI
    OpenClawBackend       OpenClaw (ACP protocol)
    CodexBackend          OpenAI Codex CLI
    OpenHandsBackend      All-Hands-AI/OpenHands (REST API)
    MockBackend           离线测试 / demo

设计原则：
1. 统一接口 — 所有 backend 实现同一个 ABC
2. 优雅降级 — 不可用 backend 通过 is_available() 表态，不抛异常
3. 零强依赖 — base/mock 仅用标准库；具体 backend 按需加载
4. 容量描述 — capabilities() 暴露 backend 特性（streaming/tools/cost）
"""

from .base import (
    AgentBackend,
    BackendCapabilities,
    BackendUnavailableError,
    SessionConfig,
    SessionSummary,
    TokenUsage,
    ToolCall,
    TurnResponse,
)
from .registry import BackendRegistry, default_registry

# 注册所有内置 backend（lazy import 避免不必要的导入失败）
def _register_builtins():
    from .mock import MockBackend
    default_registry.register("mock", MockBackend)

    try:
        from .hermes import HermesBackend
        default_registry.register("hermes", HermesBackend)
    except ImportError:
        pass

    try:
        from .claude_code import ClaudeCodeBackend
        default_registry.register("claude_code", ClaudeCodeBackend)
    except ImportError:
        pass

    try:
        from .openclaw import OpenClawBackend
        default_registry.register("openclaw", OpenClawBackend)
    except ImportError:
        pass

    try:
        from .codex import CodexBackend
        default_registry.register("codex", CodexBackend)
    except ImportError:
        pass

    try:
        from .openhands import OpenHandsBackend
        default_registry.register("openhands", OpenHandsBackend)
    except ImportError:
        pass


_register_builtins()

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "BackendRegistry",
    "BackendUnavailableError",
    "SessionConfig",
    "SessionSummary",
    "TokenUsage",
    "ToolCall",
    "TurnResponse",
    "default_registry",
]
