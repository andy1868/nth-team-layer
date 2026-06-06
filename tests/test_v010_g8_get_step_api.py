"""G-8 (Voss audit): MissionStore.get_step is the canonical single-step
lookup.

The original MissionRunner.complete() did ``self.store.get(mission_id)
.get_step(step_id)`` - inlining the implementation. The new
``MissionStore.get_step(mission_id, step_id)`` is the abstraction
boundary: today it still loads the whole mission file, but tomorrow
it can be backed by a per-step index without breaking callers.
"""

from __future__ import annotations

import pytest

from nth_dao.orchestration.mission import Mission, MissionStep, StepStatus
from nth_dao.orchestration.mission_runner import MissionRunner
from nth_dao.orchestration.mission_store import MissionStore


def test_G8_get_step_returns_the_requested_step(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {"id": "s1", "description": "one"},
        {"id": "s2", "description": "two"},
        {"id": "s3", "description": "three"},
    ])
    store.save(mission)

    step = store.get_step(mission.id, "s2")
    assert step is not None
    assert step.id == "s2"
    assert step.description == "two"


def test_G8_get_step_returns_None_for_missing_mission(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    assert store.get_step("nonexistent_mission", "s1") is None


def test_G8_get_step_returns_None_for_missing_step(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {"id": "s1", "description": "x"},
    ])
    store.save(mission)
    assert store.get_step(mission.id, "no_such_step") is None


def test_G8_runner_uses_get_step_path(tmp_path):
    """End-to-end: MissionRunner.complete() now goes through the
    get_step abstraction. We don't intercept the call - we just
    confirm the happy path still works after the refactor."""
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "s1", "description": "x",
            "acceptance_criteria": {"min_length": 5},
        },
    ])
    store.save(mission)

    runner = MissionRunner(store=store, agent_id="alice")
    runner.claim(mission.id, "s1")
    outcome = runner.complete(
        mission_id=mission.id, step_id="s1",
        output={"content": "ok long enough"},
    )
    assert outcome.success is True
