"""Tests for examples/nth_telegram_bot.py lazy-init contract.

The bot module must be importable without TELEGRAM_BOT_TOKEN or
DEEPSEEK_API_KEY set, so that:
    - pytest can collect tests in the examples/ tree
    - static analyzers (mypy, ruff) can parse the file
    - tooling like docs-generation can introspect handlers

`_validate_env()` is only called from main(). `get_llm()` is only
called from handlers when DeepSeek is actually needed.

Lazy-init pattern originally proposed by @andy1868 in PR #7.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Strip all telegram/DeepSeek env vars AND redirect ``~/.hermes/.env``
    away from the dev box so the bot's `_load_dotenv` at import time
    can't undo monkeypatch's work.
    """
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS",
                "DEEPSEEK_API_KEY", "KEDELAW_BOT_TOKEN", "KEDELAW_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)
    # Point Path.home() at a clean tmp dir so ~/.hermes/.env doesn't exist.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Force a fresh import every time, so _load_dotenv re-runs against the
    # redirected HOME and the lazy `_validate_env` is the only source of truth.
    sys.modules.pop("nth_telegram_bot", None)
    yield


def _import_bot():
    if str(EXAMPLES_DIR) not in sys.path:
        sys.path.insert(0, str(EXAMPLES_DIR))
    if "openai" not in sys.modules:
        pytest.importorskip("openai")
    if "telegram" not in sys.modules:
        pytest.importorskip("telegram")
    return importlib.import_module("nth_telegram_bot")


def test_module_imports_without_env_vars(clean_env):
    """Import must not raise — no SystemExit, no RuntimeError, no print."""
    mod = _import_bot()
    assert hasattr(mod, "_validate_env")
    assert hasattr(mod, "get_llm")
    assert hasattr(mod, "get_runtime")
    assert hasattr(mod, "main")


def test_validate_env_raises_with_clear_message(clean_env):
    mod = _import_bot()
    with pytest.raises(RuntimeError) as excinfo:
        mod._validate_env()
    msg = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in msg
    assert "DEEPSEEK_API_KEY" in msg
    assert "~/.hermes/.env" in msg


def test_validate_env_passes_when_vars_present(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    mod = _import_bot()
    mod._validate_env()  # must not raise


def test_get_llm_lazy_errors_without_key(clean_env):
    mod = _import_bot()
    # Reset singleton between tests
    mod._LLM = None
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        mod.get_llm()


def test_get_llm_caches_client(monkeypatch, clean_env):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    mod = _import_bot()
    mod._LLM = None
    client1 = mod.get_llm()
    client2 = mod.get_llm()
    assert client1 is client2


def test_no_module_level_side_effects(clean_env):
    """Smoke test: importing the module twice must not double-initialize."""
    mod1 = _import_bot()
    mod2 = _import_bot()
    assert mod1 is mod2
    # Singletons untouched by import alone
    assert mod1._TEAM is None
    assert mod1._LLM is None
