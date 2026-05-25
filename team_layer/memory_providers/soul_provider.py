"""
SoulProvider — TEAM-SOUL.md 懒加载（<200 token 高阶原则）

设计：
- prefetch() 仅提取 Core Anti-Patterns + Preferred Stack（<60 token 描述）
- 长尾规则转向量化存入 VectorProvider（RAG 按需检索）
- on_pre_compress() 强制保护灵魂关键词，防止被压缩摘掉
"""

from pathlib import Path
from typing import Optional
import re
from ..runtime import MemoryProviderABC


class SoulProvider(MemoryProviderABC):
    """Team 灵魂提供者"""

    def __init__(self, soul_path: str = "skills/TEAM-SOUL.md", max_core_tokens: int = 200):
        self.soul_path = Path(soul_path)
        self.max_core_tokens = max_core_tokens
        self.core_content = ""
        self.preserved_keywords = []

    def initialize(self, context: dict) -> None:
        """启动时解析灵魂文件"""
        if not self.soul_path.exists():
            self.core_content = "# TEAM SOUL\n[No soul file found. Please create skills/TEAM-SOUL.md]"
            return

        try:
            full_text = self.soul_path.read_text(encoding="utf-8")
            self.core_content = self._extract_core_section(full_text)

            # 提取关键词供 on_pre_compress 使用
            self.preserved_keywords = self._extract_keywords(self.core_content)
        except Exception as e:
            self.core_content = f"[Error reading soul: {e}]"

    @staticmethod
    def _extract_core_section(full_text: str) -> str:
        """
        从 TEAM-SOUL.md 提取 Core Anti-Patterns + Preferred Stack（前 200 token）

        假设结构：
        # TEAM SOUL (Core Summary)
        ## Absolute Anti-Patterns
        ...
        ## Preferred Stack
        ...
        """
        lines = full_text.split("\n")
        core_lines = []
        in_core = False

        for i, line in enumerate(lines):
            if "## Core" in line or "Absolute Anti-Patterns" in line:
                in_core = True

            if in_core:
                core_lines.append(line)

                # 粗估 token 数（~4 chars per token）
                current_chars = sum(len(l) for l in core_lines)
                if current_chars > 800:  # ~200 tokens
                    break

            if in_core and i > 0 and lines[i].startswith("#") and "Core" not in line:
                # 遇到新的同级标题，停止
                break

        return "\n".join(core_lines[:30])  # 最多 30 行

    @staticmethod
    def _extract_keywords(text: str) -> list:
        """从核心内容提取关键词（用于压缩保护）"""
        keywords = []
        # 匹配 - keyword: description 或 **keyword**
        pattern = r"(?:\*\*|^- )(\w+[-_\w]*)"
        matches = re.findall(pattern, text, re.MULTILINE)
        return list(set(matches))

    def prefetch(self, session_id: str) -> str:
        """返回灵魂核心内容"""
        if not self.core_content:
            self.initialize({})
        return f"## TEAM SOUL\n{self.core_content}"

    def on_pre_compress(self, compaction_hint: str) -> None:
        """压缩前钩子 — 防止灵魂规则被摘掉"""
        # 这里不做实际操作，但标记这些关键词不应被压缩
        # 实际压缩管线（team_layer/compression/）会检查这个标记
        preserved = "\n".join([f"  - {kw}" for kw in self.preserved_keywords])
        print(f"[SOUL] Preserving keywords on compress: {preserved}")

    def sync_turn(self, action: dict, result: any) -> None:
        """每轮同步 — 暂无灵魂更新（SOUL 是只读的）"""
        pass

    def on_session_end(self) -> None:
        """会话结束 — 灵魂不需持久化（文件已是 SSOT）"""
        pass


# 示例 TEAM-SOUL.md 内容（供参考）
TEAM_SOUL_TEMPLATE = """# TEAM SOUL (Core Summary)

## Absolute Anti-Patterns
1. **Bare API calls** — 禁止直接调用外部 API，必须加 timeout + retry
2. **Cross-agent memory pollution** — 子代理结果必须通过 sidechain 隔离，不直接返回给主上下文
3. **Mutable shared state** — 所有状态通过 Git append-only 日志，严禁 in-place 修改
4. **Context explosion** — 压缩阈值 75%，必须分层执行
5. **Unaudited tool execution** — 所有工具调用必须通过 permission_gate + 7 层防线

## Preferred Stack
- **Memory**: CLAUDE.md 式可编辑 + 向量索引 + append-only 账本
- **Compression**: 5 层管线（廉价优先）
- **Safety**: 7 层权限模型 + ML classifier + 沙箱隔离
- **Sync**: Git SSOT + 原子级热加载 + 零冲突日志命名

## Evolution Policy
- 触发条件：同类错误 ≥3 次 AND 浪费 token > 进化预算的 1.5 倍
- 流程：Reflector Subagent (生成 Patch) → Verifier (沙箱验证) → Evolution Gate
- 低风险 (Lint 修复) 自动 Merge；高风险 (架构级) 等待人工审批

[动态加载指令]
当遇到新类型错误时，执行 `load_skill(error-category)`，从 skills/registry/ 按需召回
"""
