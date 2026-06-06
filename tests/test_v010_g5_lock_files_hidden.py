"""G-5 (Voss audit): safe_append_jsonl puts lock files in a hidden
``.locks/`` subdirectory rather than sprawling them alongside data
files.

Before the fix every JSONL had a sibling ``<name>.jsonl.lock`` -
which polluted ``ls`` output, made rsync/git noisy, and made stale
locks invisible (one lock per file = many places to look).

After the fix all locks land in ``<dir>/.locks/<name>.jsonl.lock``,
visible to ops in one place and excludable from backups with a
single glob.
"""

from __future__ import annotations

from pathlib import Path

from nth_dao.util import safe_append_jsonl


def test_G5_lock_file_lives_in_dot_locks_subdir(tmp_path):
    target = tmp_path / "events.jsonl"
    safe_append_jsonl(target, {"i": 1})

    # The lock must NOT be next to the data file
    sibling_lock = target.with_suffix(target.suffix + ".lock")
    assert not sibling_lock.exists()

    # It should be in the hidden subdir instead
    assert (tmp_path / ".locks" / "events.jsonl.lock").exists()


def test_G5_data_directory_stays_clean(tmp_path):
    """Multiple JSONL files share a single .locks subdir - the data
    directory itself only contains data."""
    safe_append_jsonl(tmp_path / "a.jsonl", {"i": 1})
    safe_append_jsonl(tmp_path / "b.jsonl", {"i": 2})
    safe_append_jsonl(tmp_path / "c.jsonl", {"i": 3})

    visible = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert visible == ["a.jsonl", "b.jsonl", "c.jsonl"], (
        f"data dir polluted with lock files: {visible}"
    )

    locks = sorted(p.name for p in (tmp_path / ".locks").iterdir())
    assert "a.jsonl.lock" in locks
    assert "b.jsonl.lock" in locks
    assert "c.jsonl.lock" in locks


def test_G5_nested_directories_get_their_own_locks_dir(tmp_path):
    """Each parent dir gets its own .locks subdir - cross-dir lock
    files don't share a namespace."""
    sub_a = tmp_path / "dir_a"
    sub_b = tmp_path / "dir_b"
    safe_append_jsonl(sub_a / "events.jsonl", {"d": "a"})
    safe_append_jsonl(sub_b / "events.jsonl", {"d": "b"})

    assert (sub_a / ".locks" / "events.jsonl.lock").exists()
    assert (sub_b / ".locks" / "events.jsonl.lock").exists()
