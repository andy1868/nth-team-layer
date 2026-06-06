"""G-11 (Voss audit): acceptance_criteria rules dispatch via a registry.

The original evaluate() had every rule inlined as a 5-20 line branch in
a 135-line method, which made:
  * adding a new rule require touching the middle of a long method
  * unit-testing a single rule require setting up a full MissionStep
  * the dispatch order an implicit property of source line ordering

G-11 extracts each rule to a module-level checker and routes through
ACCEPTANCE_RULE_REGISTRY. This pins the registry contract:

  * Registry is an ordered tuple of (rule_name, checker) pairs
  * Order matches the documented rule order (required_keys ->
    min_length -> must_contain -> forbidden -> regex -> max_tokens)
  * Each checker is callable in isolation - no MissionStep needed
  * Checker returns None on pass, str fragment on fail
  * Each failure fragment contains the rule name (substring assertions
    in older tests keep working)
  * Registry is immutable (tuple, not list) so it can't be mutated
    at runtime by accident

Importantly: behaviour of evaluate() itself is unchanged - G-10
aggregation and G-3/G-4 robustness all still hold. The 46 tests in
PR-3/G-3/G-4/G-10 form the behavioural regression net.
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import (
    ACCEPTANCE_RULE_REGISTRY,
    _check_forbidden,
    _check_max_tokens,
    _check_min_length,
    _check_must_contain,
    _check_regex,
    _check_required_keys,
)


# ===== registry shape =====


def test_G11_registry_is_immutable_tuple():
    """Tuple, not list - prevents downstream code from monkey-patching
    a new rule into evaluation at runtime."""
    assert isinstance(ACCEPTANCE_RULE_REGISTRY, tuple)
    for entry in ACCEPTANCE_RULE_REGISTRY:
        assert isinstance(entry, tuple)
        assert len(entry) == 2
        name, checker = entry
        assert isinstance(name, str)
        assert callable(checker)


def test_G11_registry_lists_exactly_the_six_documented_rules():
    """The registry is the source of truth for which rules
    evaluate() recognises. If it drifts from the docstring, dashboards
    and mission-template validators see the wrong set."""
    names = [name for name, _ in ACCEPTANCE_RULE_REGISTRY]
    assert names == [
        "required_keys",
        "min_length",
        "must_contain",
        "forbidden",
        "regex",
        "max_tokens",
    ]


def test_G11_registry_order_matches_documented_aggregation_order():
    """G-10 documents that aggregated reason fragments appear in
    registry order. The registry IS that order - so this test pins
    them together: drift the registry, this test fails, fixing it
    fixes both."""
    expected_order = (
        "required_keys", "min_length", "must_contain",
        "forbidden", "regex", "max_tokens",
    )
    actual_order = tuple(name for name, _ in ACCEPTANCE_RULE_REGISTRY)
    assert actual_order == expected_order


# ===== per-checker contracts =====


def test_G11_check_required_keys_passes_when_all_keys_present():
    assert _check_required_keys(
        ["a", "b"], {"a": 1, "b": 2, "c": 3}, "",
    ) is None


def test_G11_check_required_keys_fails_with_missing_listed():
    msg = _check_required_keys(
        ["a", "b"], {"a": 1}, "",
    )
    assert msg is not None
    assert "required_keys" in msg
    assert "'b'" in msg


def test_G11_check_min_length_passes_at_exact_threshold():
    """min_length is a >= check, not >."""
    assert _check_min_length(5, {}, "12345") is None


def test_G11_check_min_length_fails_below():
    msg = _check_min_length(10, {}, "short")
    assert msg is not None
    assert "min_length" in msg
    assert "10" in msg and "5" in msg


def test_G11_check_must_contain_passes_when_all_present():
    assert _check_must_contain(
        ["foo", "bar"], {}, "foo and bar here",
    ) is None


def test_G11_check_must_contain_fails_with_absent_listed():
    msg = _check_must_contain(
        ["foo", "bar"], {}, "only foo here",
    )
    assert msg is not None
    assert "must_contain" in msg
    assert "bar" in msg


def test_G11_check_forbidden_passes_when_none_present():
    assert _check_forbidden(
        ["secret", "password"], {}, "safe content",
    ) is None


def test_G11_check_forbidden_fails_with_present_listed():
    msg = _check_forbidden(
        ["secret", "password"], {}, "the password is 1234",
    )
    assert msg is not None
    assert "forbidden" in msg
    assert "password" in msg


def test_G11_check_regex_passes_on_match():
    assert _check_regex(r"\d+", {}, "abc 123 def") is None


def test_G11_check_regex_fails_on_no_match():
    msg = _check_regex(r"\d+", {}, "no digits")
    assert msg is not None
    assert "regex" in msg


def test_G11_check_regex_fails_on_non_string_pattern():
    """G-4 contract: non-string pattern reported as eval failure."""
    msg = _check_regex(12345, {}, "x")
    assert msg is not None
    assert "must be a string" in msg


def test_G11_check_regex_fails_on_oversize_pattern():
    """G-4 contract: pattern length cap enforced."""
    msg = _check_regex("a" * 2000, {}, "x")
    assert msg is not None
    assert "exceeds" in msg and "cap" in msg


def test_G11_check_regex_fails_on_compile_error():
    """G-4 contract: compile errors surface as eval failures, not raise."""
    msg = _check_regex("(unbalanced", {}, "x")
    assert msg is not None
    assert "regex compile failed" in msg


def test_G11_check_max_tokens_passes_under_limit():
    assert _check_max_tokens(100, {"tokens_used": 50}, "") is None


def test_G11_check_max_tokens_fails_over_limit():
    msg = _check_max_tokens(100, {"tokens_used": 200}, "")
    assert msg is not None
    assert "max_tokens" in msg
    assert "200" in msg and "100" in msg


def test_G11_check_max_tokens_fails_on_non_numeric():
    """G-3 contract: non-numeric tokens_used reported, not crashes."""
    msg = _check_max_tokens(100, {"tokens_used": "3.5k"}, "")
    assert msg is not None
    assert "not numeric" in msg


def test_G11_check_max_tokens_missing_field_defaults_to_zero():
    """G-3 contract: agent didn't report -> 0 -> trivially under limit."""
    assert _check_max_tokens(100, {}, "") is None


# ===== invariant: checker None means pass =====


@pytest.mark.parametrize("checker, rule_value, output, content", [
    (_check_required_keys, ["a"],         {"a": 1},        ""),
    (_check_min_length,    1,             {},              "x"),
    (_check_must_contain,  ["x"],         {},              "x"),
    (_check_forbidden,     ["q"],         {},              "x"),
    (_check_regex,         r"\w",         {},              "x"),
    (_check_max_tokens,    100,           {"tokens_used": 1}, ""),
])
def test_G11_checkers_return_none_on_pass(checker, rule_value, output, content):
    """Universal pass contract: every checker returns None (not "",
    not True, not the rule name) when the rule passes."""
    result = checker(rule_value, output, content)
    assert result is None


@pytest.mark.parametrize("checker, rule_value, output, content, expected_in", [
    (_check_required_keys, ["x"],         {},              "",     "required_keys"),
    (_check_min_length,    100,           {},              "x",    "min_length"),
    (_check_must_contain,  ["X"],         {},              "",     "must_contain"),
    (_check_forbidden,     ["X"],         {},              "X",    "forbidden"),
    (_check_regex,         r"NOMATCH",    {},              "x",    "regex"),
    (_check_max_tokens,    1,             {"tokens_used": 99}, "", "max_tokens"),
])
def test_G11_checkers_return_str_with_rule_name_on_fail(
    checker, rule_value, output, content, expected_in,
):
    """Universal fail contract: every checker returns a str fragment,
    and that fragment contains its rule name."""
    result = checker(rule_value, output, content)
    assert isinstance(result, str)
    assert expected_in in result
