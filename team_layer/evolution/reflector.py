"""
Reflector — 生成修复 Patch 的 Subagent

输入：sidechain 中的失败日志
输出：Patch（SKILL.md + Pydantic 契约 + 元数据）

设计：
- Subagent 隔离 — 不污染主上下文（实际是独立函数，结果通过 Patch 对象传出）
- LLM 可选 — 注册 llm_callback 后用 LLM 生成；否则用模板降级
- 替代 Z3 — 用 Pydantic Schema 校验输入输出契约（轻量、可运行）
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class Patch:
    """Reflector 输出的修复 Patch"""
    skill_id: str                          # 唯一 id，作为文件名
    error_sig: str                         # 触发此 patch 的错误签名
    description: str                       # <60 token 描述
    trigger_pattern: str                   # 正则匹配触发条件
    risk_level: str                        # "low" / "medium" / "high"
    fix_steps: List[str]                   # 修复步骤
    contract: Dict[str, Dict[str, str]]    # Pydantic 契约 {input: {field: type}, output: {...}}
    sample_failures: List[str] = field(default_factory=list)  # 触发此 patch 的样本日志
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    generator: str = "template"            # "template" 或 "llm"

    def to_skill_md(self) -> str:
        """渲染为 skills/registry/*.md 格式"""
        contract_yaml = "\n".join([
            f"  {kind}: {json.dumps(fields)}"
            for kind, fields in self.contract.items()
        ])
        steps = "\n".join([f"{i+1}. {s}" for i, s in enumerate(self.fix_steps)])

        return f"""id: {self.skill_id}
desc: "{self.description}"
trigger: "{self.trigger_pattern}"
risk: {self.risk_level}
error_sig: "{self.error_sig}"
generated_at: "{self.generated_at}"
generator: {self.generator}
contract:
{contract_yaml}

## 修复步骤
{steps}

## 触发样本
{chr(10).join(['- ' + s[:80] for s in self.sample_failures[:3]])}
"""

    def to_dict(self) -> dict:
        return asdict(self)


class Reflector:
    """生成修复 Patch 的 Subagent"""

    # 错误签名到风险等级的映射（启发式）
    RISK_HEURISTICS = {
        "timeout": "low",
        "retry": "low",
        "rate_limit": "low",
        "connection_refused": "low",
        "lint": "low",
        "import": "low",
        "permission": "high",      # 权限相关 → 高风险
        "auth": "high",
        "destructive": "high",
        "drop_table": "high",
        "rm_rf": "high",
    }

    def __init__(self, llm_callback: Optional[Callable] = None):
        """
        Args:
            llm_callback: 可选的 LLM 调用函数 fn(prompt: str) -> str
                          签名: 接收提示词，返回 JSON 字符串
                          若为 None，使用模板降级
        """
        self.llm_callback = llm_callback

    def reflect(
        self,
        error_sig: str,
        sample_logs: List[dict],
    ) -> Patch:
        """
        基于错误签名和样本日志生成 Patch

        Args:
            error_sig: 错误签名（如 "timeout_database"）
            sample_logs: 触发此错误的样本日志条目（ledger 中的 entry）

        Returns:
            Patch 对象，包含完整的修复 skill + 契约
        """
        sample_results = [str(log.get("result", ""))[:100] for log in sample_logs]

        if self.llm_callback:
            try:
                return self._llm_reflect(error_sig, sample_logs, sample_results)
            except Exception as e:
                print(f"[REFLECTOR] LLM failed ({e}), falling back to template")

        return self._template_reflect(error_sig, sample_results)

    def _template_reflect(self, error_sig: str, sample_results: List[str]) -> Patch:
        """模板化生成 Patch（无 LLM 时的降级路径）"""
        risk = self._infer_risk(error_sig)
        skill_id = f"fix_{re.sub(r'[^a-z0-9_]', '_', error_sig.lower())}"

        # 根据 error_sig 类型生成对应修复策略
        fix_steps = self._template_fix_steps(error_sig)

        return Patch(
            skill_id=skill_id,
            error_sig=error_sig,
            description=f"Auto-generated fix for {error_sig}",
            trigger_pattern=re.escape(error_sig).replace(r"_", r".*"),
            risk_level=risk,
            fix_steps=fix_steps,
            contract={
                "input": {"code": "str", "error": "str"},
                "output": {"patched_code": "str", "applied": "bool"},
            },
            sample_failures=sample_results,
            generator="template",
        )

    def _llm_reflect(
        self,
        error_sig: str,
        sample_logs: List[dict],
        sample_results: List[str],
    ) -> Patch:
        """用 LLM 生成 Patch"""
        prompt = self._build_llm_prompt(error_sig, sample_logs)
        response = self.llm_callback(prompt)

        # 解析 LLM 返回的 JSON
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                raise ValueError("LLM response is not valid JSON")
            data = json.loads(match.group(0))

        return Patch(
            skill_id=data.get("skill_id", f"fix_{error_sig.lower()}"),
            error_sig=error_sig,
            description=data.get("description", f"LLM fix for {error_sig}"),
            trigger_pattern=data.get("trigger_pattern", error_sig),
            risk_level=data.get("risk_level", self._infer_risk(error_sig)),
            fix_steps=data.get("fix_steps", []),
            contract=data.get("contract", {
                "input": {"code": "str"},
                "output": {"patched_code": "str"},
            }),
            sample_failures=sample_results,
            generator="llm",
        )

    def _infer_risk(self, error_sig: str) -> str:
        """启发式判断风险等级"""
        sig_lower = error_sig.lower()
        for keyword, risk in self.RISK_HEURISTICS.items():
            if keyword in sig_lower:
                return risk
        return "medium"  # 未知类型 → 中风险（保守）

    @staticmethod
    def _template_fix_steps(error_sig: str) -> List[str]:
        """根据 error_sig 关键字推断修复步骤模板"""
        sig_lower = error_sig.lower()

        if "timeout" in sig_lower or "connection" in sig_lower:
            return [
                "定位发生超时的调用点",
                "引入 tenacity 库或等价重试机制",
                "添加 @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1))",
                "确保 timeout 参数显式设置（避免无限阻塞）",
            ]
        if "rate_limit" in sig_lower:
            return [
                "定位被限流的 API 调用",
                "添加指数退避：wait_exponential(min=1, max=60)",
                "检查是否需要降低并发度",
            ]
        if "import" in sig_lower or "module" in sig_lower:
            return [
                "检查 import 是否在 try/except 中保护",
                "添加 lazy import 或 importlib 降级路径",
                "在 requirements 中固定版本",
            ]
        if "lint" in sig_lower:
            return [
                "运行格式化工具（ruff/black）",
                "修复 lint 警告（行长、未使用变量等）",
            ]
        if "permission" in sig_lower or "auth" in sig_lower:
            return [
                "[HIGH RISK] 检查 permission_gate 配置",
                "确认 token/key 未过期",
                "考虑增加 7 层权限审计",
                "等待人工审批",
            ]

        return [
            f"分析 {error_sig} 的根因",
            "添加防御性代码（try/except + 日志）",
            "补充单元测试覆盖此场景",
        ]

    @staticmethod
    def _build_llm_prompt(error_sig: str, sample_logs: List[dict]) -> str:
        """构建 LLM 提示词"""
        samples = "\n".join([
            f"  - {json.dumps(log)[:200]}"
            for log in sample_logs[:5]
        ])
        return f"""You are a Reflector subagent. Analyze recurring failure and generate a fix patch.

Error signature: {error_sig}
Sample failures from ledger:
{samples}

Generate a JSON patch with this exact schema:
{{
  "skill_id": "fix_<short_name>",
  "description": "<60-token description>",
  "trigger_pattern": "<regex matching this error>",
  "risk_level": "low" | "medium" | "high",
  "fix_steps": ["step1", "step2", ...],
  "contract": {{
    "input": {{"field": "type"}},
    "output": {{"field": "type"}}
  }}
}}

Return ONLY valid JSON, no preamble."""
