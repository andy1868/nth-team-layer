"""
BackendRegistry — 注册中心 + 自动发现

用法：
    from team_layer.backends import default_registry

    # 列出所有已注册的 backend
    default_registry.list_all()
    # → ['mock', 'hermes', 'claude_code', 'openclaw', 'codex', 'openhands']

    # 列出当前环境可用的（is_available()=True）
    default_registry.list_available()
    # → ['mock', 'claude_code']  (依赖具体机器)

    # 实例化
    backend = default_registry.create("mock", model="mock-fast")

    # 第三方注册自定义 backend
    default_registry.register("custom", MyCustomBackend)
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Type

from .base import AgentBackend, BackendUnavailableError


@dataclass
class BackendInfo:
    """Registry 元数据"""
    backend_id: str
    cls: Type[AgentBackend]
    is_available: Optional[bool] = None  # 缓存的可用性
    error: Optional[str] = None           # 不可用时的原因


class BackendRegistry:
    """全局 backend 注册表"""

    def __init__(self):
        self._backends: Dict[str, BackendInfo] = {}

    def register(self, backend_id: str, backend_cls: Type[AgentBackend]) -> None:
        """注册一个 backend 类（idempotent — 重复注册覆盖）"""
        if not issubclass(backend_cls, AgentBackend):
            raise TypeError(f"{backend_cls} must subclass AgentBackend")
        # 设置 backend_id（如未设置）
        if not getattr(backend_cls, "backend_id", None) or backend_cls.backend_id == "abstract":
            backend_cls.backend_id = backend_id
        self._backends[backend_id] = BackendInfo(
            backend_id=backend_id,
            cls=backend_cls,
        )

    def get(self, backend_id: str) -> Type[AgentBackend]:
        """获取 backend 类（未注册抛 KeyError）"""
        if backend_id not in self._backends:
            raise KeyError(
                f"backend {backend_id!r} not registered. "
                f"Available: {sorted(self._backends.keys())}"
            )
        return self._backends[backend_id].cls

    def create(self, backend_id: str, **kwargs) -> AgentBackend:
        """实例化一个 backend"""
        cls = self.get(backend_id)
        if not cls.is_available(**kwargs):
            raise BackendUnavailableError(
                f"backend {backend_id!r} is registered but not available "
                f"in current environment"
            )
        return cls(**kwargs)

    def try_create(self, backend_id: str, **kwargs) -> Optional[AgentBackend]:
        """实例化，失败返回 None（不抛异常）"""
        try:
            return self.create(backend_id, **kwargs)
        except (KeyError, BackendUnavailableError):
            return None

    def list_all(self) -> List[str]:
        """所有已注册的 backend ID"""
        return sorted(self._backends.keys())

    def list_available(self, refresh: bool = False, **probe_kwargs) -> List[str]:
        """当前环境可用的 backend ID（探测 is_available）"""
        result = []
        for bid, info in self._backends.items():
            if refresh or info.is_available is None:
                try:
                    info.is_available = info.cls.is_available(**probe_kwargs)
                except Exception as e:
                    info.is_available = False
                    info.error = f"{type(e).__name__}: {e}"
            if info.is_available:
                result.append(bid)
        return sorted(result)

    def describe(self, refresh: bool = False) -> Dict[str, Dict]:
        """完整状态报告（供 CLI / 诊断使用）"""
        out = {}
        for bid, info in self._backends.items():
            if refresh or info.is_available is None:
                try:
                    info.is_available = info.cls.is_available()
                except Exception as e:
                    info.is_available = False
                    info.error = f"{type(e).__name__}: {e}"

            # 尝试获取 capabilities
            try:
                instance = info.cls()
                caps = instance.capabilities()
                caps_dict = {
                    "supports_streaming": caps.supports_streaming,
                    "supports_tools": caps.supports_tools,
                    "max_context_tokens": caps.max_context_tokens,
                    "notes": caps.notes,
                }
            except Exception:
                caps_dict = {}

            out[bid] = {
                "available": bool(info.is_available),
                "error": info.error,
                "class": f"{info.cls.__module__}.{info.cls.__name__}",
                "capabilities": caps_dict,
            }
        return out


# 全局默认实例
default_registry = BackendRegistry()
