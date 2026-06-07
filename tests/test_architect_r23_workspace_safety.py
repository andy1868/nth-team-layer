"""Architect R-23 (2026-06-08): workspace defaults must not leak the
private key into a committed git tree.

Pre-fix the default workspace resolution was ``Path.cwd()``. The
typical operator flow ``cd nth-team-layer && python -m nth_dao.web``
landed identity.json (containing the plaintext Ed25519 private key)
INSIDE the source tree. One ``git add -A`` and the key goes public.

Pins:
  * default workspace lives under ``~/.nth-dao/workspaces/default/``,
    NOT in the cwd
  * explicit workspace argument still wins
  * ``NTH_WORKSPACE`` env var still wins
  * workspace inside a git tree triggers exactly one WARNING log
  * .gitignore must exclude every sensitive file pattern
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import nth_dao.web as web_mod
from nth_dao.web import _resolve_safe_workspace


# ===== default resolution =====


def test_R23_default_workspace_is_NOT_cwd(monkeypatch, tmp_path):
    """The most important property: when no caller / env override is
    set, the workspace MUST live somewhere other than ``Path.cwd()``."""
    monkeypatch.delenv("NTH_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_safe_workspace(None)
    assert resolved != tmp_path
    # And it lives under ~/.nth-dao/ specifically
    assert ".nth-dao" in resolved.parts


def test_R23_default_workspace_resolves_under_home(monkeypatch):
    monkeypatch.delenv("NTH_WORKSPACE", raising=False)
    resolved = _resolve_safe_workspace(None)
    assert resolved.is_absolute()
    assert str(Path.home()) in str(resolved)


def test_R23_default_workspace_is_created_if_missing(monkeypatch, tmp_path):
    """The default lives under HOME, but the directory itself may not
    exist on first boot. We must create it - otherwise WebState
    initialization would fail trying to mkdir inside a non-existent
    workspace."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.delenv("NTH_WORKSPACE", raising=False)
    resolved = _resolve_safe_workspace(None)
    assert resolved.exists()
    assert resolved.is_dir()


# ===== explicit overrides still win =====


def test_R23_explicit_workspace_arg_wins(tmp_path):
    resolved = _resolve_safe_workspace(tmp_path)
    assert resolved == tmp_path.resolve()


def test_R23_NTH_WORKSPACE_env_wins_when_no_explicit_arg(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NTH_WORKSPACE", str(tmp_path))
    resolved = _resolve_safe_workspace(None)
    assert resolved == tmp_path.resolve()


def test_R23_explicit_arg_beats_env(monkeypatch, tmp_path):
    """When both env and explicit arg are supplied, the explicit arg
    is authoritative - matches Python's standard precedence for
    function args."""
    env_path = tmp_path / "env"
    arg_path = tmp_path / "arg"
    env_path.mkdir()
    arg_path.mkdir()
    monkeypatch.setenv("NTH_WORKSPACE", str(env_path))
    resolved = _resolve_safe_workspace(arg_path)
    assert resolved == arg_path.resolve()


# ===== git tree warning =====


def test_R23_git_tree_inside_root_triggers_warning(
    tmp_path, caplog,
):
    """When the workspace sits inside a checkout (the typical danger
    case), we emit exactly one WARNING with a clear remediation hint."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "workspace"
    sub.mkdir()
    with caplog.at_level(logging.WARNING, logger="nth_dao.web"):
        _resolve_safe_workspace(sub)
    git_warnings = [
        r for r in caplog.records
        if "sits inside a git checkout" in r.message
    ]
    assert len(git_warnings) == 1
    # The remediation hint mentions both the safe paths
    msg = git_warnings[0].message
    assert "NTH_WORKSPACE" in msg or ".gitignore" in msg


def test_R23_no_warning_when_workspace_outside_any_git_tree(
    tmp_path, caplog,
):
    """A clean workspace (no .git anywhere above it) is the happy
    path and must not nag."""
    workspace = tmp_path / "clean_workspace"
    workspace.mkdir()
    with caplog.at_level(logging.WARNING, logger="nth_dao.web"):
        _resolve_safe_workspace(workspace)
    git_warnings = [
        r for r in caplog.records
        if "git checkout" in r.message
    ]
    # No warning when the workspace really is outside a checkout.
    # (We can't fully control whether the test tmpdir itself sits
    # under one - the test_R23_git_tree_inside_root_triggers_warning
    # above gives the positive evidence. Here we just confirm the
    # warning doesn't fire spuriously when there's clearly no .git.)
    assert len(git_warnings) == 0


def test_R23_explicit_workspace_under_git_still_works(tmp_path, caplog):
    """Warn-only posture: even though the workspace IS inside a git
    tree, we proceed - tests, dev mode, and intentional inside-tree
    deployments all need this. The warning is the safety net."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "ws"
    sub.mkdir()
    with caplog.at_level(logging.WARNING, logger="nth_dao.web"):
        resolved = _resolve_safe_workspace(sub)
    assert resolved == sub.resolve()


# ===== .gitignore coverage =====


REPO_ROOT = Path(__file__).resolve().parent.parent
SENSITIVE_PATHS_TO_VERIFY = [
    # path relative to repo root that ``git check-ignore`` must say is ignored
    ".nth/identity.json",
    "identity.json",
    "team.json",
    "console.token",
    "team_contacts/contacts.jsonl",
    "team_marketplace/some-order.json",
]


@pytest.mark.parametrize("path", SENSITIVE_PATHS_TO_VERIFY)
def test_R23_gitignore_excludes_sensitive_path(path):
    """Run ``git check-ignore`` on each sensitive path. Exit 0 means
    git IS ignoring it. Exit 1 means it would be staged. We want 0."""
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-v", path],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"git would NOT ignore {path!r}; add a rule to .gitignore. "
        f"check-ignore output: {result.stdout!r} {result.stderr!r}"
    )


def test_R23_gitignore_does_not_block_legitimate_source_files():
    """Negative sanity: identity.json IS blocked, but
    nth_dao/contact_book.py (a normal source file) must NOT be."""
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-v", "nth_dao/contact_book.py"],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
    )
    # exit 1 = NOT ignored, which is what we want for source files
    assert result.returncode == 1, (
        f"git is incorrectly ignoring a normal source file. "
        f"output: {result.stdout!r}"
    )
