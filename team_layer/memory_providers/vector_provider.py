"""
VectorProvider — 向量知识库（长尾规则 + 技能索引）

设计：
- SQLite-vec 存储（可选）
- 长尾规则动态向量化，按需 RAG 检索
- 技能库索引：从 skills/registry/ 加载 .md 文件
- 不提交 Git（本地生成，跨终端通过技能库同步）
"""

import json
from pathlib import Path
from typing import List, Dict, Any
from ..runtime import MemoryProviderABC


class VectorProvider(MemoryProviderABC):
    """向量知识库提供者"""

    def __init__(self, skills_dir: str = "skills/registry", db_path: str = "memory/vectors.jsonl"):
        self.skills_dir = Path(skills_dir)
        self.db_path = Path(db_path)
        self.skill_index = []  # [{name, desc, path}]

    def initialize(self, context: dict) -> None:
        """启动时加载技能库索引"""
        if not self.skills_dir.exists():
            print(f"[WARN] Skills dir not found: {self.skills_dir}")
            return

        # 扫描所有 .md 文件
        for skill_file in self.skills_dir.glob("*.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                # 简单解析：提取 id: 和 desc: 字段
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
        """从 YAML 前置数据提取字段"""
        for line in text.split("\n")[:50]:
            if f"{field_name}:" in line:
                # 简单解析 key: "value" 或 key: value
                _, _, value = line.partition(":")
                return value.strip().strip('"')
        return ""

    def prefetch(self, session_id: str) -> str:
        """返回技能库概览（不包含完整内容）"""
        if not self.skill_index:
            return "## Skills Library\n[No skills indexed]"

        content = "## Available Skills\n"
        for skill in self.skill_index[:10]:  # 最多 10 个
            content += f"- **{skill['name']}**: {skill['desc'][:60]}...\n"

        if len(self.skill_index) > 10:
            content += f"\n... and {len(self.skill_index) - 10} more skills\n"

        return content

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, str]]:
        """
        检索相关技能（简单关键字匹配，后续可升级为向量搜索）

        Args:
            query: 搜索查询
            top_k: 返回数量

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

        # 按分数排序
        scored.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in scored[:top_k]]

    def load_skill_content(self, skill_name: str) -> str:
        """加载技能的完整内容"""
        skill_path = self.skills_dir / f"{skill_name}.md"
        if skill_path.exists():
            return skill_path.read_text(encoding="utf-8")
        return f"[Skill {skill_name} not found]"

    def sync_turn(self, action: dict, result: Any) -> None:
        """每轮同步 — 可记录新的学习技能（暂无）"""
        pass

    def on_pre_compress(self, compaction_hint: str) -> None:
        """压缩前 — 向量库不需要特殊保护"""
        pass

    def on_session_end(self) -> None:
        """会话结束 — 向量库持久化（如果有新增技能）"""
        # 暂无新增，略过
        pass
