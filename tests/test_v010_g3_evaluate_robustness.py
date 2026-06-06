"""G-3 (Voss audit): evaluate() must not crash on malformed inputs.

The original max_tokens rule did:

    used = int(output.get("tokens_used", 0))

which propagated TypeError/ValueError out of evaluate() if the
agent's output had a non-numeric tokens_used field. MissionRunner.
complete() would then crash, leaving the step stuck mid-flight.

The fix: catch the conversion error and return it as a normal
evaluation failure with a clear reason.
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import Mission, MissionStep, StepStatus
from nth_dao.orchestration.mission_runner import MissionRunner
from nth_dao.orchestration.mission_store import MissionStore


@pytest.mark.parametrize("bad_value", [
    None,
    "3.5k",
    "lots",
    {"prompt": 10, "completion": 5},
    [1, 2, 3],
    object(),
])
def test_G3_max_tokens_rejects_non_numeric_without_crashing(bad_value):
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"max_tokens": 100},
    )
    # Pre-fix this would have raised TypeError/ValueError
    ok, reason = step.evaluate({"content": "x", "tokens_used": bad_value})
    assert ok is False
    assert "numeric" in reason or "not" in reason.lower()


def test_G3_max_tokens_accepts_int_castable_string():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"max_tokens": 100},
    )
    # "50" is castable to 50 via int() - allowed
    ok, _ = step.evaluate({"content": "x", "tokens_used": "50"})
    assert ok is True


def test_G3_max_tokens_still_enforces_the_limit():
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"max_tokens": 100},
    )
    ok, reason = step.evaluate({"content": "x", "tokens_used": 200})
    assert ok is False
    assert "200" in reason and "100" in reason


def test_G3_missing_tokens_used_defaults_to_zero():
    """Backward compat: when the rule is set but the agent didn't
    report tokens_used, default to 0 (which trivially satisfies
    any positive max_tokens)."""
    step = MissionStep(
        id="s1", description="x",
        acceptance_criteria={"max_tokens": 100},
    )
    ok, _ = step.evaluate({"content": "x"})
    assert ok is True


def test_G3_runner_complete_does_not_crash_on_bad_tokens_used(tmp_path):
    """End-to-end: MissionRunner.complete() with a buggy agent that
    emits tokens_used as a dict still returns a clean RunnerOutcome
    (success=False) instead of propagating the type error."""
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "s1", "description": "x",
            "acceptance_criteria": {"max_tokens": 1000},
        },
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="buggy_agent")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "x", "tokens_used": {"prompt": 100, "completion": 50}},
    )
    assert outcome.success is False
    # The step transitions to NEEDS_REVIEW (G-3 failure goes through
    # the same gate as any other evaluate failure).
    final = store.get(mission.id).get_step("s1")
    assert final.status == StepStatus.NEEDS_REVIEW.value
