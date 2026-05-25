"""
EvoLoop 自进化引擎

流水线：
    Trigger (ROI 滞后) → Reflector (生成 Patch) → Verifier (沙箱验证) → Gate (Merge/Pending)

设计原则：
1. 严禁基于 LLM 的"未来预测"，只看历史硬指标（Trigger）
2. Subagent 隔离 — Reflector/Verifier 不污染主上下文
3. 双重校验 — 沙箱运行 + Pydantic 契约
4. 风险分级 — Low 自动 Merge，High 等待人工审批
"""

from .trigger import EvoTrigger, EvolutionDecision
from .reflector import Reflector, Patch
from .verifier import Verifier, VerifyResult
from .gate import EvolutionGate, GateDecision, RiskLevel
from .orchestrator import EvoLoop

__all__ = [
    "EvoTrigger",
    "EvolutionDecision",
    "Reflector",
    "Patch",
    "Verifier",
    "VerifyResult",
    "EvolutionGate",
    "GateDecision",
    "RiskLevel",
    "EvoLoop",
]
