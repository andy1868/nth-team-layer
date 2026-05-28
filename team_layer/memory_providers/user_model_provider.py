"""
UserModelProvider


- /
- Bayesian
- on_pre_compress()
"""

import json
from pathlib import Path
from typing import Dict, Any
from datetime import datetime
from ..runtime import MemoryProviderABC


class UserModelProvider(MemoryProviderABC):
    """"""

    def __init__(self, model_path: str = "memory/user-model.json"):
        self.model_path = Path(model_path)
        self.preferences = {}  # {key: weight}
        self.decision_history = []  # [(action, accepted, reason)]

    def initialize(self, context: dict) -> None:
        """"""
        if self.model_path.exists():
            try:
                data = json.loads(self.model_path.read_text())
                self.preferences = data.get("preferences", {})
                self.decision_history = data.get("history", [])[-50:]  #  50
            except Exception as e:
                print(f"[WARN] Failed to load user model: {e}")

    def prefetch(self, session_id: str) -> str:
        """Top 5"""
        if not self.preferences:
            return "## User Model\n[No preferences learned yet]"

        #  Top 5
        top_prefs = sorted(
            self.preferences.items(), key=lambda x: x[1], reverse=True
        )[:5]

        content = "## User Preferences (Top 5)\n"
        for key, weight in top_prefs:
            content += f"- {key}: {weight:.2f}\n"

        return content

    def record_decision(self, action: dict, accepted: bool, reason: str = "") -> None:
        """  """
        action_type = action.get("type", "unknown")

        # Bayesian
        if action_type not in self.preferences:
            self.preferences[action_type] = 0.0

        if accepted:
            self.preferences[action_type] += 0.1
        else:
            self.preferences[action_type] -= 0.05

        #  [-1, 1]
        self.preferences[action_type] = max(-1.0, min(1.0, self.preferences[action_type]))

        #
        self.decision_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": action_type,
            "accepted": accepted,
            "reason": reason,
        })

    def sync_turn(self, action: dict, result: Any) -> None:
        """ record_decision """
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """  """
        top_prefs = sorted(self.preferences.items(), key=lambda x: x[1], reverse=True)[:3]
        keywords = [p[0] for p in top_prefs]
        print(f"[USER_MODEL] Protecting preferences: {keywords}")

    def on_session_end(self) -> None:
        """  """
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "preferences": self.preferences,
            "history": self.decision_history[-100:],  #  100
            "last_update": datetime.now().isoformat(),
        }
        self.model_path.write_text(json.dumps(data, indent=2))
        print(f"[USER_MODEL] Saved to {self.model_path}")
