"""
5 层上下文压缩管线（廉价优先）

流程：
1. Budget Reduction ($0) — 降低 effort_level
2. Snip History ($0) — 截断巨大输出
3. Microcompact ($0.001) — 压缩最后 1-2 轮为单句
4. Context Collapse ($0.01) — 合并过去 5 轮为摘要
5. Auto-compact Summary ($0.05) — 调用 LLM 对全量历史做摘要，preserved-tail 机制

触发阈值：
- 50%: Budget Reduction
- 60%: Snip History
- 70%: Microcompact
- 75%: Context Collapse
- 85%: Auto-compact Summary（硬限制）
"""

from enum import Enum
from typing import List, Tuple, Any, Callable, Optional
import re


class CompressionStage(Enum):
    """压缩阶段枚举"""
    BUDGET_REDUCTION = 1
    SNIP_HISTORY = 2
    MICROCOMPACT = 3
    CONTEXT_COLLAPSE = 4
    AUTO_COMPACT_SUMMARY = 5


class CompressionPipeline:
    """5 层压缩管线"""

    def __init__(
        self,
        history: List[Tuple[dict, Any]],
        max_history_chars: int = 50000,  # 粗估
        effort_level: str = "high",
    ):
        self.history = history
        self.max_history_chars = max_history_chars
        self.effort_level = effort_level

    def estimate_context_usage(self) -> float:
        """粗估上下文占用率（基于字符数）"""
        total_chars = sum(
            len(str(action)) + len(str(result))
            for action, result in self.history
        )
        return min(1.0, total_chars / self.max_history_chars)

    def should_compress(self, threshold: float = 0.75) -> Optional[CompressionStage]:
        """判断需要哪一级压缩"""
        usage = self.estimate_context_usage()

        if usage < 0.50:
            return None
        elif usage < 0.60:
            return CompressionStage.BUDGET_REDUCTION
        elif usage < 0.70:
            return CompressionStage.SNIP_HISTORY
        elif usage < 0.75:
            return CompressionStage.MICROCOMPACT
        elif usage < 0.85:
            return CompressionStage.CONTEXT_COLLAPSE
        else:
            return CompressionStage.AUTO_COMPACT_SUMMARY

    def execute_budget_reduction(self) -> str:
        """Stage 1: 降低 effort_level（$0 成本）"""
        old_level = self.effort_level
        self.effort_level = "low"
        msg = f"[COMPRESS] Stage 1: Reduced effort_level from {old_level} to low"
        print(msg)
        return msg

    def execute_snip_history(self, max_output_len: int = 5000) -> str:
        """Stage 2: 截断巨大的 tool output（$0 成本）"""
        snipped_count = 0
        for i, (action, result) in enumerate(self.history[-10:]):
            result_str = str(result)
            if len(result_str) > max_output_len:
                snipped_len = len(result_str)
                self.history[i] = (action, f"[SNIPPED: {snipped_len} chars, first 100: {result_str[:100]}...]")
                snipped_count += 1

        msg = f"[COMPRESS] Stage 2: Snipped {snipped_count} large outputs"
        print(msg)
        return msg

    def execute_microcompact(self) -> str:
        """Stage 3: 压缩最后 1-2 轮为单句（$0.001）"""
        if len(self.history) < 2:
            return "[COMPRESS] Stage 3: Not enough history to microcompact"

        last_action, last_result = self.history[-1]
        action_type = last_action.get("type", "unknown")
        result_preview = str(last_result)[:50]

        summary = f"Executed {action_type}, result: {result_preview}"
        self.history[-1] = (last_action, summary)

        msg = f"[COMPRESS] Stage 3: Microcompacted last round: {summary}"
        print(msg)
        return msg

    def execute_context_collapse(self, window_size: int = 5) -> str:
        """Stage 4: 合并过去 N 轮为摘要（$0.01）"""
        if len(self.history) < window_size:
            return f"[COMPRESS] Stage 4: Not enough history (need {window_size}, have {len(self.history)})"

        # 取最后 N 轮，生成文字摘要
        recent_rounds = self.history[-window_size:]
        action_types = [a.get("type", "?") for a, _ in recent_rounds]

        summary = f"[Collapsed {len(recent_rounds)} rounds: {', '.join(set(action_types))}]"

        # 替换这 N 条为 1 条摘要
        self.history = self.history[:-window_size] + [({"type": "summary"}, summary)]

        msg = f"[COMPRESS] Stage 4: Collapsed {window_size} rounds into summary"
        print(msg)
        return msg

    def execute_auto_compact_summary(self, summarizer: Optional[Callable] = None) -> str:
        """
        Stage 5: 调用 LLM 对全量历史做摘要 + preserved-tail（$0.05）

        preserved-tail 机制：丢弃前 95% 原文，保留最近 3 轮高保真交互
        """
        if summarizer is None:
            summarizer = self._default_summarizer

        try:
            # 构建历史文本
            history_text = "\n".join([
                f"Action: {a}\nResult: {str(r)[:100]}"
                for a, r in self.history
            ])

            # 调用 LLM 摘要
            summary = summarizer(history_text)

            # preserved-tail：保留最近 3 轮
            tail_size = 3
            tail_rounds = self.history[-tail_size:]

            # 清空历史，只保留摘要 + tail
            self.history = [
                ({"type": "summary"}, summary)
            ] + tail_rounds

            msg = f"[COMPRESS] Stage 5: Auto-compacted with preserved-tail (kept last {tail_size} rounds)"
            print(msg)
            return msg

        except Exception as e:
            msg = f"[COMPRESS] Stage 5 failed: {e}, using fallback"
            print(msg)
            return msg

    @staticmethod
    def _default_summarizer(history_text: str) -> str:
        """默认摘要函数（后续替换为实际 LLM 调用）"""
        lines = history_text.split("\n")[:20]
        return f"[Auto-summary of {len(lines)} actions]"

    def execute(self, stage: CompressionStage) -> str:
        """执行指定的压缩阶段"""
        if stage == CompressionStage.BUDGET_REDUCTION:
            return self.execute_budget_reduction()
        elif stage == CompressionStage.SNIP_HISTORY:
            return self.execute_snip_history()
        elif stage == CompressionStage.MICROCOMPACT:
            return self.execute_microcompact()
        elif stage == CompressionStage.CONTEXT_COLLAPSE:
            return self.execute_context_collapse()
        elif stage == CompressionStage.AUTO_COMPACT_SUMMARY:
            return self.execute_auto_compact_summary()
        else:
            return f"[COMPRESS] Unknown stage: {stage}"

    def auto_compress(self, threshold: float = 0.75) -> str:
        """自动判断并执行合适的压缩"""
        stage = self.should_compress(threshold)
        if stage is None:
            return f"[COMPRESS] Context usage {self.estimate_context_usage():.1%}, no compression needed"

        print(f"[COMPRESS] Context usage: {self.estimate_context_usage():.1%}, triggering {stage.name}")
        return self.execute(stage)
