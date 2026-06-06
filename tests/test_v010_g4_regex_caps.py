"""G-4 (Voss audit): evaluate() regex rule defence-in-depth.

Python's ``re`` engine has no timeout and is vulnerable to ReDoS via
patterns like ``(a+)+`` on adversarial content. The full mitigation
is a trust-boundary policy (acceptance_criteria must come from the
mission owner, never an untrusted runtime caller), but as
defence-in-depth we cap:

  * pattern length at 1024 chars
  * content length at 64 KiB

Plus we report compile errors as evaluation failures instead of
crashing the runner.
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import MissionStep


def test_G4_oversize_regex_pattern_rejected():
    """A 2 KiB pattern is well past any reasonable use and is the
    signature of a hand-crafted bomb. Reject with a clear reason."""
    huge_pattern = "a" * 2000
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": huge_pattern},
    )
    ok, reason = step.evaluate({"content": "anything"})
    assert ok is False
    assert "exceeds" in reason and "cap" in reason


def test_G4_regex_compile_error_returned_as_evaluation_failure():
    """Invalid regex (unbalanced paren etc.) must surface as ok=False
    with a clear reason, NOT propagate out as re.error."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": "(unbalanced"},
    )
    ok, reason = step.evaluate({"content": "anything"})
    assert ok is False
    assert "regex compile failed" in reason


def test_G4_non_string_regex_rejected():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": 12345},
    )
    ok, reason = step.evaluate({"content": "x"})
    assert ok is False
    assert "must be a string" in reason


def test_G4_valid_regex_still_works():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": r"\d{4}-\d{2}-\d{2}"},
    )
    ok, _ = step.evaluate({"content": "shipped on 2026-06-06"})
    assert ok is True


def test_G4_content_truncated_to_64kib_for_regex():
    """A 100 KiB content with the match in the first 64 KiB still
    matches; with the match only past 64 KiB the rule fails.
    Documents the content cap."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": "MARKER"},
    )
    # Match in the first 64 KiB - matches
    short_content = "MARKER" + ("x" * (64 * 1024))
    ok, _ = step.evaluate({"content": short_content})
    assert ok is True

    # Match only past 64 KiB - misses (acceptable defence cost)
    long_content = ("x" * (70 * 1024)) + "MARKER"
    ok2, _ = step.evaluate({"content": long_content})
    assert ok2 is False
