"""PR-2 + PR-3: platform-aware mission filtering + acceptance criteria.

PR-2 (failure mode #1, "environment heterogeneity"):

  * attach() captures sys.platform / arch / runtime into the agent
    registry record under metadata.env
  * MissionStep gains required_platform: List[str]
  * Mission.next_actionable(agent_platform=...) filters incompatible
    steps so Linux-only work never gets offered to a Windows agent

PR-3 (failure mode #5, "task results not verified"):

  * MissionStep gains acceptance_criteria: Optional[Dict]
  * MissionStep.evaluate(output) returns (ok, reason)
  * StepStatus.NEEDS_REVIEW is a new terminal-but-reopenable state
  * MissionRunner.complete() routes failing output to NEEDS_REVIEW
    instead of DONE, preserving the original output for a reviewer
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nth_dao.orchestration.mission import (
    Mission,
    MissionStep,
    StepStatus,
)


# =====================================================================
# PR-2: env metadata + required_platform filtering
# =====================================================================


def test_PR2_capture_env_metadata_includes_required_keys():
    from nth_dao.attach import _capture_env_metadata
    meta = _capture_env_metadata()
    # PR-2's original four keys are the stable string-typed contract.
    # G-14 extended the schema with richer-typed keys (cpu_count int,
    # memory_gb float|None, gpu_* bool/int/str/None) - those are
    # NOT covered by this string invariant.
    _PR2_ORIGINAL_STRING_KEYS = (
        "platform", "architecture", "python_version", "runtime",
    )
    assert set(_PR2_ORIGINAL_STRING_KEYS) <= set(meta)
    for k in _PR2_ORIGINAL_STRING_KEYS:
        v = meta[k]
        assert isinstance(v, str) and v != "", f"{k}={v!r}"
    # Platform is normalized to lowercase
    assert meta["platform"] == meta["platform"].lower()


def test_PR2_attach_writes_env_into_registry_metadata(tmp_path):
    from nth_dao.attach import attach
    session = attach(
        agent_id="env-agent",
        backend=None,
        workspace=str(tmp_path),
        start_heartbeat=False,
        skip_preflight=True,
    )
    try:
        record = session.registry.get(session.agent_id)
        assert record is not None
        env = (record.metadata or {}).get("env", {})
        assert "platform" in env
        assert env["platform"] in ("linux", "darwin", "windows")
    finally:
        session.detach()


def test_PR2_step_without_required_platform_accepts_any_agent():
    """Backward compat: a step with no platform restriction must
    still be claimable by any agent."""
    step = MissionStep(id="s1", description="any-platform work")
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {"id": "s1", "description": "any-platform work"},
    ])
    actionable = mission.next_actionable(agent_platform="linux")
    assert any(s.id == "s1" for s in actionable)
    actionable_windows = mission.next_actionable(agent_platform="windows")
    assert any(s.id == "s1" for s in actionable_windows)


def test_PR2_step_with_required_platform_filters_out_incompatible():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "linux-only",
            "description": "shell scripts",
            "required_platform": ["linux"],
        },
        {
            "id": "any-platform",
            "description": "python work",
        },
    ])
    # Windows agent: only "any-platform" is offered
    actionable_win = mission.next_actionable(agent_platform="windows")
    ids = {s.id for s in actionable_win}
    assert "linux-only" not in ids
    assert "any-platform" in ids
    # Linux agent: both
    actionable_linux = mission.next_actionable(agent_platform="linux")
    assert {"linux-only", "any-platform"} <= {s.id for s in actionable_linux}


def test_PR2_required_platform_match_is_case_insensitive():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "macos",
            "description": "AppleScript",
            "required_platform": ["Darwin"],   # mixed case
        },
    ])
    # Agent says "darwin" lowercase - should still match
    actionable = mission.next_actionable(agent_platform="darwin")
    assert any(s.id == "macos" for s in actionable)


def test_PR2_no_agent_platform_supplied_skips_platform_filter():
    """When the caller doesn't pass agent_platform, the filter is
    inactive (backward compat for old callers)."""
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "linux-only",
            "description": "x",
            "required_platform": ["linux"],
        },
    ])
    actionable = mission.next_actionable()
    assert any(s.id == "linux-only" for s in actionable)


def test_PR2_step_round_trips_required_platform_through_json():
    """The new field must survive to_dict/from_dict cycling."""
    step = MissionStep(
        id="s1", description="x", required_platform=["linux", "darwin"],
    )
    raw = step.to_dict()
    rebuilt = MissionStep.from_dict(raw)
    assert rebuilt.required_platform == ["linux", "darwin"]


# =====================================================================
# PR-3: acceptance_criteria + evaluate() + NEEDS_REVIEW
# =====================================================================


def test_PR3_step_with_no_criteria_always_passes():
    """Backward compat: legacy missions without acceptance_criteria
    keep accepting any output, including None."""
    step = MissionStep(id="s1", description="x")
    ok, reason = step.evaluate({"content": "anything"})
    assert ok is True
    ok, _ = step.evaluate(None)
    assert ok is True
    ok, _ = step.evaluate({})
    assert ok is True


def test_PR3_min_length_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"min_length": 20},
    )
    ok, reason = step.evaluate({"content": "too short"})
    assert ok is False and "min_length" in reason
    ok, _ = step.evaluate({"content": "this is long enough to satisfy the rule"})
    assert ok is True


def test_PR3_must_contain_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"must_contain": ["TODO", "DONE"]},
    )
    ok, reason = step.evaluate({"content": "Done but no Todo"})
    assert ok is False and "TODO" in reason
    ok, _ = step.evaluate({"content": "TODO complete, then DONE"})
    assert ok is True


def test_PR3_forbidden_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"forbidden": ["password", "secret"]},
    )
    ok, reason = step.evaluate({"content": "the password is 1234"})
    assert ok is False and "forbidden" in reason
    ok, _ = step.evaluate({"content": "safe content"})
    assert ok is True


def test_PR3_regex_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"regex": r"\d{4}-\d{2}-\d{2}"},
    )
    ok, _ = step.evaluate({"content": "on 2026-06-06 we shipped"})
    assert ok is True
    ok, reason = step.evaluate({"content": "no date at all"})
    assert ok is False and "regex" in reason


def test_PR3_required_keys_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"required_keys": ["summary", "diff"]},
    )
    ok, reason = step.evaluate({"summary": "x"})
    assert ok is False and "diff" in reason
    ok, _ = step.evaluate({"summary": "x", "diff": "..."})
    assert ok is True


def test_PR3_max_tokens_rule():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"max_tokens": 100},
    )
    ok, _ = step.evaluate({"content": "x", "tokens_used": 50})
    assert ok is True
    ok, reason = step.evaluate({"content": "x", "tokens_used": 200})
    assert ok is False and "max_tokens" in reason


def test_PR3_rules_combined():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={
            "min_length": 10,
            "must_contain": ["OK"],
            "forbidden": ["FAIL"],
        },
    )
    ok, _ = step.evaluate({"content": "Status: OK and all green here"})
    assert ok is True


def test_PR3_non_dict_output_when_criteria_set_fails():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"min_length": 5},
    )
    ok, reason = step.evaluate("just a string")    # type: ignore[arg-type]
    assert ok is False and "must be a dict" in reason


def test_PR3_is_open_includes_needs_review():
    step = MissionStep(
        id="s1", description="x",
        status=StepStatus.NEEDS_REVIEW.value,
    )
    assert step.is_open is True


def test_PR3_step_round_trips_acceptance_criteria_through_json():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"min_length": 5, "must_contain": ["X"]},
    )
    raw = step.to_dict()
    rebuilt = MissionStep.from_dict(raw)
    assert rebuilt.acceptance_criteria == {
        "min_length": 5, "must_contain": ["X"],
    }


# =====================================================================
# PR-3: MissionRunner.complete() routes failing output to NEEDS_REVIEW
# =====================================================================


def test_PR3_runner_complete_routes_failing_output_to_needs_review(tmp_path):
    from nth_dao.orchestration.mission_store import MissionStore
    from nth_dao.orchestration.mission_runner import MissionRunner

    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "s1", "description": "write report",
            "acceptance_criteria": {"min_length": 100},
        },
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="alice")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "too short"},
    )
    assert outcome.success is False
    assert "needs_review" in outcome.note

    # Confirm on disk
    refreshed = store.get(mission.id)
    step = refreshed.get_step("s1")
    assert step.status == StepStatus.NEEDS_REVIEW.value
    assert step.output == {"content": "too short"}    # output preserved


def test_PR3_runner_complete_promotes_acceptable_output_to_done(tmp_path):
    from nth_dao.orchestration.mission_store import MissionStore
    from nth_dao.orchestration.mission_runner import MissionRunner

    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "s1", "description": "ship",
            "acceptance_criteria": {"must_contain": ["shipped"]},
        },
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="alice")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "feature shipped to production"},
    )
    assert outcome.success is True
    refreshed = store.get(mission.id)
    assert refreshed.get_step("s1").status == StepStatus.DONE.value


def test_PR3_runner_complete_no_criteria_is_backward_compatible(tmp_path):
    """Legacy missions without acceptance_criteria keep working
    exactly as before - any output transitions the step to DONE."""
    from nth_dao.orchestration.mission_store import MissionStore
    from nth_dao.orchestration.mission_runner import MissionRunner

    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {"id": "s1", "description": "anything"},
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="alice")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "x"},
    )
    assert outcome.success is True
    refreshed = store.get(mission.id)
    assert refreshed.get_step("s1").status == StepStatus.DONE.value
