"""
VectorProvider   +


- SQLite-vec
-  RAG
-  skills/registry/  .md
-  Git
"""

import json
from pathlib import Path
from typing import List, Dict, Any
from ..runtime import MemoryProviderABC


class VectorProvider(MemoryProviderABC):
    """"""

    def __init__(self, skills_dir: str = "skills/registry", db_path: str = "memory/vectors.jsonl"):
        self.skills_dir = Path(skills_dir)
        self.db_path = Path(db_path)
        self.skill_index = []  # [{name, desc, path}]

    def initialize(self, context: dict) -> None:
        """"""
        if not self.skills_dir.exists():
            print(f"[WARN] Skills dir not found: {self.skills_dir}")
            return

        #  .md
        for skill_file in self.skills_dir.glob("*.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                #  id:  desc:
                desc = self._extract_field(content, "desc") or skill_file.stem
                self.skill_index.append({
                    "name": skill_file.stem,
                    "desc": desc,
                    "path": str(skill_file),
                })
            except Exception as e:
                print(f"[WARN] Failed to load skill {skill_file}: {e}")

    @staticmethod
    def _extract_field(text: str, field_name: str) -> str:
        """ YAML """
        for line in text.split("\n")[:50]:
            if f"{field_name}:" in line:
                #  key: "value"  key: value
                _, _, value = line.partition(":")
                return value.strip().strip('"')
        return ""

    def prefetch(self, session_id: str) -> str:
        """"""
        if not self.skill_index:
            return "## Skills Library\n[No skills indexed]"

        content = "## Available Skills\n"
        for skill in self.skill_index[:10]:  #  10
            content += f"- **{skill['name']}**: {skill['desc'][:60]}...\n"

        if len(self.skill_index) > 10:
            content += f"\n... and {len(self.skill_index) - 10} more skills\n"

        return content

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, str]]:
        """


        Args:
            query:
            top_k:

        Returns:
            [{"name": ..., "desc": ..., "path": ...}]
        """
        query_words = query.lower().split()
        scored = []

        for skill in self.skill_index:
            score = 0
            skill_text = (skill["name"] + " " + skill["desc"]).lower()
            for word in query_words:
                if word in skill_text:
                    score += 1
            if score > 0:
                scored.append((skill, score))

        #
        scored.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in scored[:top_k]]

    def load_skill_content(self, skill_name: str) -> str:
        """"""
        skill_path = self.skills_dir / f"{skill_name}.md"
        if skill_path.exists():
            return skill_path.read_text(encoding="utf-8")
        return f"[Skill {skill_name} not found]"

    def sync_turn(self, action: dict, result: Any) -> None:
        """  """
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """  """
        pass

    def on_session_end(self) -> None:
        """  """
        #
        pass
