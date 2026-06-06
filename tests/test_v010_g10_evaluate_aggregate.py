"""G-10 (Voss audit): MissionStep.evaluate() aggregates ALL rule failures.

The original evaluate() short-circuited on the first failing rule, so
a re-submitting agent would fix one problem only to discover on the
next NEEDS_REVIEW round that two more existed. Aggregating gives the
resubmitter a complete punch-list on the first round - one of the
explicit goals of PR-3's review-loop design.

Pinned invariants:
  * Multi-rule failures all surface in the reason string
  * Each individual rule fragment still contains its own rule name
    (so existing substring assertions keep working)
  * Joiner is "; "
  * Order of fragments matches documented rule order:
    required_keys -> min_length -> must_contain -> forbidden ->
    regex -> max_tokens
  * Single-rule failure preserves the old single-line reason shape
  * All-pass still returns (True, "ok")
  * Non-dict output is a STRUCTURAL prereq, NOT aggregated - it
    short-circuits because per-rule checks all assume dict output
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import MissionStep


# ===== aggregation core =====


def test_G10_multiple_rule_failures_all_appear_in_reason():
    """min_length + must_contain + forbidden all fail simultaneously.
    Every rule's failure fragment must appear in the reason string."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 100,
            "must_contain": ["MARKER_A", "MARKER_B"],
            "forbidden": ["SECRET"],
        },
    )
    ok, reason = step.evaluate({"content": "tiny SECRET content"})
    assert ok is False
    # All three rules failed -> all three names appear
    assert "min_length" in reason
    assert "must_contain" in reason
    assert "forbidden" in reason


def test_G10_failures_are_joined_with_semicolon_and_space():
    """The aggregation joiner is documented as '; ' - tooling parsing
    the reason string (linters, dashboards) depends on this."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 100,
            "forbidden": ["SECRET"],
        },
    )
    ok, reason = step.evaluate({"content": "SECRET"})
    assert ok is False
    assert "; " in reason
    # Exactly two fragments -> exactly one joiner
    assert reason.count("; ") == 1


def test_G10_order_of_fragments_matches_documented_rule_order():
    """When required_keys + min_length + must_contain + forbidden +
    regex + max_tokens ALL fail, the fragments appear in the order
    documented in the docstring. Stable ordering matters for diff-able
    test snapshots and dashboard rendering."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "required_keys": ["summary"],
            "min_length": 100,
            "must_contain": ["MARKER"],
            "forbidden": ["BAD"],
            "regex": r"\d{4}-\d{2}-\d{2}",
            "max_tokens": 10,
        },
    )
    ok, reason = step.evaluate({"content": "tiny BAD", "tokens_used": 999})
    assert ok is False
    # Find each rule's position by its name keyword and assert ordering
    pos_required_keys = reason.find("required_keys")
    pos_min_length = reason.find("min_length")
    pos_must_contain = reason.find("must_contain")
    pos_forbidden = reason.find("forbidden")
    pos_regex = reason.find("regex")
    pos_max_tokens = reason.find("max_tokens")
    # All present
    assert -1 not in (
        pos_required_keys, pos_min_length, pos_must_contain,
        pos_forbidden, pos_regex, pos_max_tokens,
    )
    # Strictly increasing
    assert (
        pos_required_keys
        < pos_min_length
        < pos_must_contain
        < pos_forbidden
        < pos_regex
        < pos_max_tokens
    )


# ===== single-rule failures stay clean =====


def test_G10_single_rule_failure_has_no_joiner():
    """Aggregation should not pollute single-failure messages with a
    spurious '; '. A single failure produces exactly one fragment."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"min_length": 100},
    )
    ok, reason = step.evaluate({"content": "short"})
    assert ok is False
    assert "; " not in reason
    assert "min_length" in reason


# ===== all-pass invariants =====


def test_G10_all_rules_pass_returns_true_ok():
    """All-pass behaviour: returns (True, "ok"). The 'ok' literal is
    the contract MissionRunner.complete() checks - don't drift."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 5, "must_contain": ["X"], "forbidden": ["Q"],
        },
    )
    ok, reason = step.evaluate({"content": "X then content"})
    assert ok is True
    assert reason == "ok"


def test_G10_no_criteria_returns_true_empty_reason():
    """No acceptance_criteria set: ok=True with empty reason (legacy
    no-rules behaviour preserved)."""
    step = MissionStep(id="s1", description="x", acceptance_criteria=None)
    ok, reason = step.evaluate({"content": "anything"})
    assert ok is True
    assert reason == ""


# ===== structural prereq stays short-circuit =====


def test_G10_non_dict_output_short_circuits_not_aggregated():
    """A non-dict output is a structural prereq - per-rule checks all
    assume dict output. Short-circuit on that one is intentional and
    must NOT switch to aggregation."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 5, "must_contain": ["X"], "forbidden": ["Q"],
        },
    )
    ok, reason = step.evaluate("just a string")  # type: ignore[arg-type]
    assert ok is False
    assert "must be a dict" in reason
    # And specifically NO per-rule fragments leaked in
    assert "min_length" not in reason
    assert "must_contain" not in reason
    assert "forbidden" not in reason


# ===== regex failure modes still report inside aggregation =====


def test_G10_regex_compile_error_aggregates_with_other_failures():
    """If the regex is malformed AND min_length also fails, BOTH the
    compile error and the length failure must surface together. The
    old short-circuit code would have hidden the length failure
    behind the compile error."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 100,
            "regex": "(unbalanced",
        },
    )
    ok, reason = step.evaluate({"content": "tiny"})
    assert ok is False
    assert "min_length" in reason
    assert "regex compile failed" in reason


def test_G10_regex_oversize_pattern_aggregates_with_other_failures():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "forbidden": ["SECRET"],
            "regex": "a" * 2000,
        },
    )
    ok, reason = step.evaluate({"content": "the SECRET stays"})
    assert ok is False
    assert "forbidden" in reason
    assert "exceeds" in reason and "cap" in reason


# ===== max_tokens failure modes still report inside aggregation =====


def test_G10_max_tokens_non_numeric_aggregates_with_other_failures():
    """max_tokens's G-3 'tokens_used is not numeric' branch must
    aggregate together with other failures. The old short-circuit
    code hid every other rule behind a malformed tokens_used."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 100,
            "max_tokens": 1000,
        },
    )
    ok, reason = step.evaluate({"content": "short", "tokens_used": "3.5k"})
    assert ok is False
    assert "min_length" in reason
    assert "not numeric" in reason
