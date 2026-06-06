"""G-15 (Voss audit): mission steps can require OS + CPU architecture.

``required_platform`` is OS-only ("linux", "darwin", "windows") and
cannot distinguish linux-x86_64 from linux-arm64. G-15 adds
``required_runtime`` with compound keys such as ``linux-x86_64``.
"""

from nth_dao.attach import _capture_env_metadata
from nth_dao.orchestration.mission import Mission, MissionStep
from nth_dao.orchestration.mission_runner import MissionRunner
from nth_dao.orchestration.mission_store import MissionStore


def test_G15_capture_env_metadata_includes_runtime_key():
    env = _capture_env_metadata()
    assert env["runtime_key"] == f"{env['platform']}-{env['architecture']}"
    assert env["runtime_key"].lower() == env["runtime_key"]


def test_G15_required_runtime_filters_incompatible_architecture():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "linux-x64",
            "description": "native binary",
            "required_runtime": ["linux-x86_64"],
        },
        {
            "id": "linux-arm",
            "description": "arm binary",
            "required_runtime": ["linux-arm64"],
        },
        {
            "id": "any",
            "description": "portable python",
        },
    ])

    actionable = mission.next_actionable(agent_runtime="linux-x86_64")
    ids = {step.id for step in actionable}
    assert "linux-x64" in ids
    assert "linux-arm" not in ids
    assert "any" in ids


def test_G15_required_runtime_match_is_case_insensitive():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "mac-arm",
            "description": "mac native",
            "required_runtime": ["Darwin-ARM64"],
        },
    ])

    actionable = mission.next_actionable(agent_runtime="darwin-arm64")
    assert [step.id for step in actionable] == ["mac-arm"]


def test_G15_no_agent_runtime_supplied_keeps_backward_compatibility():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "native",
            "description": "native binary",
            "required_runtime": ["linux-x86_64"],
        },
    ])

    actionable = mission.next_actionable()
    assert [step.id for step in actionable] == ["native"]


def test_G15_required_runtime_round_trips_through_json():
    step = MissionStep(
        id="s1",
        description="x",
        required_runtime=["linux-x86_64", "darwin-arm64"],
    )

    rebuilt = MissionStep.from_dict(step.to_dict())
    assert rebuilt.required_runtime == ["linux-x86_64", "darwin-arm64"]


def test_G15_required_platform_and_runtime_both_apply():
    mission = Mission.new(title="T", goal="G", owner="alice", steps=[
        {
            "id": "linux-x64",
            "description": "native binary",
            "required_platform": ["linux"],
            "required_runtime": ["linux-x86_64"],
        },
    ])

    assert not mission.next_actionable(
        agent_platform="darwin",
        agent_runtime="linux-x86_64",
    )
    assert not mission.next_actionable(
        agent_platform="linux",
        agent_runtime="linux-arm64",
    )
    assert mission.next_actionable(
        agent_platform="linux",
        agent_runtime="linux-x86_64",
    )


def test_G15_mission_store_filters_shared_work_by_runtime(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "x64",
            "description": "native binary",
            "required_runtime": ["linux-x86_64"],
        },
    ])
    store.save(mission)

    assert not store.list_for_agent(
        "alice",
        agent_runtime="linux-arm64",
        include_team=True,
    )
    assert store.list_for_agent(
        "alice",
        agent_runtime="linux-x86_64",
        include_team=True,
    )


def test_G15_mission_runner_uses_runtime_filter(tmp_path):
    store = MissionStore(str(tmp_path / "missions"))
    mission = Mission.new(title="T", goal="G", owner="planner", steps=[
        {
            "id": "arm",
            "description": "arm binary",
            "required_runtime": ["linux-arm64"],
        },
        {
            "id": "x64",
            "description": "x64 binary",
            "required_runtime": ["linux-x86_64"],
        },
    ])
    store.save(mission)

    runner = MissionRunner(
        store=store,
        agent_id="alice",
        runtime="linux-x86_64",
    )
    work = runner.find_work()
    assert work is not None
    _, step = work
    assert step.id == "x64"
