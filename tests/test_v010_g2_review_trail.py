"""G-2 (Voss audit): NEEDS_REVIEW must not destroy the first
submitter's work.

The original PR-3 implementation overwrote step.output whenever a
new agent submitted, so:

  agent_A completes with output_A → fails acceptance → NEEDS_REVIEW
  agent_B claims the same step, completes with output_B → DONE

...would lose output_A entirely. The audit chain would have no record
of what agent_A submitted.

The fix: a per-step ``review_trail`` list. Every rejected submission
is APPENDED to the trail (timestamp, submitter, output, reason). The
current ``output`` field is still the latest attempt; the trail
preserves history.
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import Mission, MissionStep, StepStatus
from nth_dao.orchestration.mission_runner import MissionRunner
from nth_dao.orchestration.mission_store import MissionStore


def _make_mission_with_strict_step(store: MissionStore) -> Mission:
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "s1",
            "description": "write the report",
            "acceptance_criteria": {"min_length": 100},
        },
    ])
    store.save(mission)
    return mission


def test_G2_review_trail_is_empty_initially():
    step = MissionStep(id="s1", description="x")
    assert step.review_trail == []


def test_G2_first_rejection_appends_to_review_trail(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    mission = _make_mission_with_strict_step(store)

    runner_a = MissionRunner(store=store, agent_id="agent_A")
    runner_a.claim(mission.id, "s1")
    outcome_a = runner_a.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "too short A"},
    )
    assert outcome_a.success is False

    refreshed = store.get(mission.id)
    step = refreshed.get_step("s1")
    assert step.status == StepStatus.NEEDS_REVIEW.value
    assert len(step.review_trail) == 1
    entry = step.review_trail[0]
    assert entry["by"] == "agent_A"
    assert entry["output"] == {"content": "too short A"}
    assert "min_length" in entry["reason"]
    assert entry["ts"]


def test_G2_second_rejection_appends_without_losing_first(tmp_path):
    """The whole point: agent_A's failed submission is preserved
    even after agent_B submits."""
    store = MissionStore(str(tmp_path / "missions"))
    mission = _make_mission_with_strict_step(store)

    # agent_A's submission fails
    runner_a = MissionRunner(store=store, agent_id="agent_A")
    runner_a.claim(mission.id, "s1")
    runner_a.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "submission from A"},
    )

    # agent_B re-claims and ALSO submits failing output
    refreshed = store.get(mission.id)
    step_after_a = refreshed.get_step("s1")
    assert step_after_a.is_open    # NEEDS_REVIEW is reclaim-able

    runner_b = MissionRunner(store=store, agent_id="agent_B")
    runner_b.claim(mission.id, "s1")
    runner_b.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "submission from B"},
    )

    final = store.get(mission.id).get_step("s1")
    # The trail must have BOTH entries
    assert len(final.review_trail) == 2
    assert final.review_trail[0]["by"] == "agent_A"
    assert final.review_trail[0]["output"] == {"content": "submission from A"}
    assert final.review_trail[1]["by"] == "agent_B"
    assert final.review_trail[1]["output"] == {"content": "submission from B"}
    # ``output`` field is the LATEST attempt (B's)
    assert final.output == {"content": "submission from B"}


def test_G2_successful_completion_does_not_add_to_review_trail(tmp_path):
    """When acceptance passes, the submission goes to ``output`` as
    DONE - it doesn't get added to review_trail (the trail is for
    REJECTED attempts only)."""
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "s1", "description": "x",
            "acceptance_criteria": {"min_length": 5},
        },
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="agent_A")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "long enough to pass"},
    )
    assert outcome.success is True

    final = store.get(mission.id).get_step("s1")
    assert final.status == StepStatus.DONE.value
    assert final.review_trail == []
    assert final.output == {"content": "long enough to pass"}


def test_G2_rejection_then_success_keeps_trail(tmp_path):
    """If agent_A fails and agent_B succeeds, the trail keeps A's
    rejected attempt as evidence the work happened."""
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "s1", "description": "x",
            "acceptance_criteria": {"min_length": 20},
        },
    ])
    store.save(mission)

    runner_a = MissionRunner(store=store, agent_id="agent_A")
    runner_a.claim(mission.id, "s1")
    runner_a.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "too short"},
    )

    runner_b = MissionRunner(store=store, agent_id="agent_B")
    runner_b.claim(mission.id, "s1")
    runner_b.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "this submission is long enough"},
    )

    final = store.get(mission.id).get_step("s1")
    assert final.status == StepStatus.DONE.value
    assert final.output == {"content": "this submission is long enough"}
    # A's rejected attempt is preserved
    assert len(final.review_trail) == 1
    assert final.review_trail[0]["by"] == "agent_A"


def test_G2_review_trail_round_trips_through_json(tmp_path):
    """The new field must survive to_dict/from_dict cycling so it
    persists across reads."""
    step = MissionStep(
        id="s1", description="x",
        review_trail=[
            {"ts": "2026-06-06T10:00:00", "by": "A", "output": {"x": 1}, "reason": "r1"},
            {"ts": "2026-06-06T11:00:00", "by": "B", "output": {"y": 2}, "reason": "r2"},
        ],
    )
    raw = step.to_dict()
    rebuilt = MissionStep.from_dict(raw)
    assert rebuilt.review_trail == step.review_trail
