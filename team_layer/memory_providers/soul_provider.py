"""
SoulProvider  TEAM-SOUL.md <200 token


- prefetch()  Core Anti-Patterns + Preferred Stack<60 token
-  VectorProviderRAG
- on_pre_compress()
"""

from pathlib import Path
from typing import Optional
import re
from ..runtime import MemoryProviderABC


class SoulProvider(MemoryProviderABC):
    """Team """

    def __init__(self, soul_path: str = "skills/TEAM-SOUL.md", max_core_tokens: int = 200):
        self.soul_path = Path(soul_path)
        self.max_core_tokens = max_core_tokens
        self.core_content = ""
        self.preserved_keywords = []

    def initialize(self, context: dict) -> None:
        """"""
        if not self.soul_path.exists():
            self.core_content = "# TEAM SOUL\n[No soul file found. Please create skills/TEAM-SOUL.md]"
            return

        try:
            full_text = self.soul_path.read_text(encoding="utf-8")
            self.core_content = self._extract_core_section(full_text)

            #  on_pre_compress
            self.preserved_keywords = self._extract_keywords(self.core_content)
        except Exception as e:
            self.core_content = f"[Error reading soul: {e}]"

    @staticmethod
    def _extract_core_section(full_text: str) -> str:
        """
         TEAM-SOUL.md  Core Anti-Patterns + Preferred Stack 200 token


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

                #  token ~4 chars per token
                current_chars = sum(len(l) for l in core_lines)
                if current_chars > 800:  # ~200 tokens
                    break

            if in_core and i > 0 and lines[i].startswith("#") and "Core" not in line:
                #
                break

        return "\n".join(core_lines[:30])  #  30

    @staticmethod
    def _extract_keywords(text: str) -> list:
        """"""
        keywords = []
        #  - keyword: description  **keyword**
        pattern = r"(?:\*\*|^- )(\w+[-_\w]*)"
        matches = re.findall(pattern, text, re.MULTILINE)
        return list(set(matches))

    def prefetch(self, session_id: str) -> str:
        """"""
        if not self.core_content:
            self.initialize({})
        return f"## TEAM SOUL\n{self.core_content}"

    def on_pre_compress(self, compaction_hint: str) -> None:
        """  """
        #
        # team_layer/compression/
        preserved = "\n".join([f"  - {kw}" for kw in self.preserved_keywords])
        print(f"[SOUL] Preserving keywords on compress: {preserved}")

    def sync_turn(self, action: dict, result: any) -> None:
        """  SOUL """
        pass

    def on_session_end(self) -> None:
        """   SSOT"""
        pass


#  TEAM-SOUL.md
TEAM_SOUL_TEMPLATE = """# TEAM SOUL (Core Summary)

## Absolute Anti-Patterns
1. **Bare API calls**   API timeout + retry
2. **Cross-agent memory pollution**   sidechain
3. **Mutable shared state**   Git append-only  in-place
4. **Context explosion**   75%
5. **Unaudited tool execution**   permission_gate + 7

## Preferred Stack
- **Memory**: CLAUDE.md  +  + append-only
- **Compression**: 5
- **Safety**: 7  + ML classifier +
- **Sync**: Git SSOT +  +

## Evolution Policy
-  3  AND  token >  1.5
- Reflector Subagent ( Patch)  Verifier ()  Evolution Gate
-  (Lint )  Merge ()

[]
 `load_skill(error-category)` skills/registry/
"""
