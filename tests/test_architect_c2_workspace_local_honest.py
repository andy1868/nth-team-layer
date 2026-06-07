"""Architect audit C-2 (2026-06-07): ``workspace_is_local`` is now
an honest filesystem probe, not a hard-coded ``True``.

The original code emitted ``"workspace_is_local": True`` as a literal
constant. While technically correct under the current in-process
architecture, the API surface read as a runtime detection - which is
exactly how downstream consumers (frontend, future remote-deployment
adapters, monitoring dashboards) would treat it.

The fix replaces the constant with ``_workspace_is_locally_accessible``,
which actually checks:
  * the path exists
  * the path is a directory
  * we can read at least one entry from it (catches stale symlinks,
    unmounted shares, permission denial)

Pinned invariants:
  * Real workspace (tmp_path) returns True
  * Missing path returns False
  * Workspace pointing at a file (not directory) returns False
  * Helper never raises (even on a path that triggers OSError on
    iterdir)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import create_app
from nth_dao.web import _workspace_is_locally_accessible


# ===== unit: helper alone =====


def test_C2_real_directory_is_local(tmp_path):
    """A regular existing directory - the happy path. Pre-fix this
    was hard-coded True; we want to confirm the honest check still
    answers True for the legitimate case."""
    assert _workspace_is_locally_accessible(tmp_path) is True


def test_C2_missing_path_is_not_local(tmp_path):
    """A non-existent path is NOT local-accessible. Pre-fix this lied."""
    missing = tmp_path / "does-not-exist"
    assert _workspace_is_locally_accessible(missing) is False


def test_C2_path_that_is_a_file_is_not_local(tmp_path):
    """If the workspace path points at a file (not a directory), we
    cannot use it as a workspace - return False."""
    file_path = tmp_path / "regular_file.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    assert _workspace_is_locally_accessible(file_path) is False


def test_C2_helper_does_not_raise_on_oserror(tmp_path, monkeypatch):
    """Even if iterdir somehow raises an OSError (e.g., simulated stale
    network mount), the helper must return False, not propagate."""
    real_iterdir = Path.iterdir

    def boom(self):
        if str(self) == str(tmp_path):
            raise OSError("simulated stale mount")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", boom)
    assert _workspace_is_locally_accessible(tmp_path) is False


# ===== integration: /api/summary uses the honest value =====


def test_C2_summary_reports_workspace_is_local_when_workspace_exists(tmp_path):
    """End-to-end: a fresh tmp_path workspace yields
    ``workspace_is_local: True`` via the real probe, not the constant."""
    client = TestClient(create_app(tmp_path))
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_is_local"] is True


def test_C2_summary_reports_false_when_workspace_becomes_inaccessible(
    tmp_path, monkeypatch,
):
    """If the probe returns False, the API surface MUST reflect it.
    This is the key behavioural difference vs the old hard-coded
    ``True`` - downstream consumers can now branch on it honestly."""
    client = TestClient(create_app(tmp_path))

    # Force the helper to return False mid-flight.
    import nth_dao.web as web_mod
    monkeypatch.setattr(
        web_mod, "_workspace_is_locally_accessible", lambda p: False,
    )
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_is_local"] is False
