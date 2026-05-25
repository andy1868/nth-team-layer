"""
UserModelProvider — 用户偏好与行为模型（跨会话学习）

设计：
- 自动沉淀用户反馈（接受/拒绝决策历史）
- 每轮更新偏好权重（Bayesian 方式）
- on_pre_compress() 保护用户关键偏好，防止被压缩摘掉
"""

import json
from pathlib import Path
from typing import Dict, Any
from datetime import datetime
from ..runtime import MemoryProviderABC


class UserModelProvider(MemoryProviderABC):
    """用户模型提供者"""

    def __init__(self, model_path: str = "memory/user-model.json"):
        self.model_path = Path(model_path)
        self.preferences = {}  # {key: weight}
        self.decision_history = []  # [(action, accepted, reason)]

    def initialize(self, context: dict) -> None:
        """启动时加载用户模型"""
        if self.model_path.exists():
            try:
                data = json.loads(self.model_path.read_text())
                self.preferences = data.get("preferences", {})
                self.decision_history = data.get("history", [])[-50:]  # 保留最近 50 条
            except Exception as e:
                print(f"[WARN] Failed to load user model: {e}")

    def prefetch(self, session_id: str) -> str:
        """返回当前用户偏好（Top 5）"""
        if not self.preferences:
            return "## User Model\n[No preferences learned yet]"

        # 按权重排序，返回 Top 5
        top_prefs = sorted(
            self.preferences.items(), key=lambda x: x[1], reverse=True
        )[:5]

        content = "## User Preferences (Top 5)\n"
        for key, weight in top_prefs:
            content += f"- {key}: {weight:.2f}\n"

        return content

    def record_decision(self, action: dict, accepted: bool, reason: str = "") -> None:
        """记录用户决策 — 用于学习偏好"""
        action_type = action.get("type", "unknown")

        # 更新权重（Bayesian 方式）
        if action_type not in self.preferences:
            self.preferences[action_type] = 0.0

        if accepted:
            self.preferences[action_type] += 0.1
        else:
            self.preferences[action_type] -= 0.05

        # 夹住在 [-1, 1]
        self.preferences[action_type] = max(-1.0, min(1.0, self.preferences[action_type]))

        # 记录历史
        self.decision_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action_type,
            "accepted": accepted,
            "reason": reason,
        })

    def sync_turn(self, action: dict, result: Any) -> None:
        """每轮同步（暂无新决策，由外部 record_decision 主动调用）"""
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """压缩前 — 标记关键偏好不能删"""
        top_prefs = sorted(self.preferences.items(), key=lambda x: x[1], reverse=True)[:3]
        keywords = [p[0] for p in top_prefs]
        print(f"[USER_MODEL] Protecting preferences: {keywords}")

    def on_session_end(self) -> None:
        """会话结束 — 持久化用户模型"""
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "preferences": self.preferences,
            "history": self.decision_history[-100:],  # 保留最近 100 条
            "last_update": datetime.now().isoformat(),
        }
        self.model_path.write_text(json.dumps(data, indent=2))
        print(f"[USER_MODEL] Saved to {self.model_path}")
