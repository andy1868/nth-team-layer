"""G-9 (Voss audit): preflight_check methods no longer import inside
the body.

The original PR-1 backends had:

    def preflight_check(self, ...):
        import subprocess
        import time as _time
        from datetime import datetime, timezone
        from .base import PreflightResult
        ...

These per-call imports were a code smell and prevented static
analysis tools from seeing the module's dependency surface. The fix
promotes them to module scope.

This test inspects the source of each backend's preflight_check
method to confirm no ``import`` statement appears in the function
body. It's an anti-regression net so the next person editing a
backend can't accidentally re-introduce the smell.
"""

from __future__ import annotations

import inspect

import pytest


_BACKENDS_WITH_PREFLIGHT = [
    "team_layer.backends.claude_code:ClaudeCodeBackend",
    "team_layer.backends.codex:CodexBackend",
    "team_layer.backends.hermes:HermesBackend",
    "team_layer.backends.openhands:OpenHandsBackend",
    "team_layer.backends.openclaw:OpenClawBackend",
]


@pytest.mark.parametrize("dotted", _BACKENDS_WITH_PREFLIGHT)
def test_G9_preflight_check_has_no_inline_imports(dotted):
    module_name, class_name = dotted.split(":")
    import importlib
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    src = inspect.getsource(cls.preflight_check)
    # Strip the docstring before checking - docstrings can mention
    # 'import' freely (e.g. "import hermes failed")
    lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith('"""')]
    body = "\n".join(lines)
    # No ``import`` statements (except inside strings / comments).
    # Heuristic: a line whose first non-whitespace token is `import` or `from`
    # AND ends with an `import` clause.
    offenders = []
    for ln in body.splitlines():
        stripped = ln.lstrip()
        # Skip docstring/comment lines
        if stripped.startswith("#"):
            continue
        if stripped.startswith("import ") or (
            stripped.startswith("from ") and " import " in stripped
        ):
            offenders.append(ln.strip())
    assert not offenders, (
        f"{dotted}.preflight_check still has inline imports: {offenders}"
    )
