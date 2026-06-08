"""A2A Task ↔ NTH Mission bridge — L1-4 (2026-06-08).

Coverage:

  1. Pure bridge functions
     * create_mission_from_subtasks: rejects empty / non-list / blank-
       string inputs; persists Mission with the right linkage metadata
     * link_existing_mission_to_task: idempotent; None on unknown id
     * mission_summary: counts every status bucket; surfaces next
       actionable step
     * enrich_task_with_mission: pure view-time mutation; broken
       linkage surfaces as ``mission_summary: None`` (consumer can
       detect)

  2. A2A RPC tasks/split end-to-end
     * Splits a Task → Mission with N steps
     * Task gets metadata.nth_mission_id
     * Subsequent tasks/get returns enriched mission summary
     * Splitting an already-linked task is rejected
     * Empty subtasks rejected
     * Splitting an unknown task returns NOT_FOUND
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nth_dao.a2a_mission_bridge import (
    META_MISSION_TASK_IDS,
    META_TASK_MISSION_ID,
    META_TASK_MISSION_STATUS,
    META_TASK_MISSION_SUMMARY,
    create_mission_from_subtasks,
    enrich_task_with_mission,
    link_existing_mission_to_task,
    mission_summary,
)
from nth_dao.a2a_rpc import (
    A2A_TASK_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
)
from nth_dao.identity import crypto_available
from nth_dao.orchestration.mission_store import MissionStore
from nth_dao.web import create_app


def _rpc(method, params=None, req_id="r1"):
    p = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        p["params"] = params
    return p


def _user_msg(text="hello", **overrides):
    msg = {"role": "ROLE_USER", "parts": [{"kind": "text", "text": text}]}
    msg.update(overrides)
    return msg


# ─── pure bridge functions ───────────────────────────────────────────


def test_create_mission_from_subtasks_persists_steps_in_order(tmp_path):
    store = MissionStore(str(tmp_path))
    mission = create_mission_from_subtasks(
        mission_store=store,
        owner="admin",
        task_id="task-xyz",
        title="Plan trip to Mars",
        goal="Land safely",
        subtasks=["Build rocket", "Test rocket", "Launch", "Land"],
    )
    assert len(mission.steps) == 4
    assert [s.description for s in mission.steps] == [
        "Build rocket", "Test rocket", "Launch", "Land",
    ]
    # Task ID surfaced on the Mission metadata
    assert mission.metadata[META_MISSION_TASK_IDS] == ["task-xyz"]
    # Reloaded from disk
    loaded = store.get(mission.id)
    assert loaded is not None
    assert len(loaded.steps) == 4


def test_create_mission_rejects_empty_subtasks(tmp_path):
    store = MissionStore(str(tmp_path))
    with pytest.raises(ValueError):
        create_mission_from_subtasks(
            mission_store=store, owner="admin",
            task_id="x", title="t", goal="g", subtasks=[],
        )


def test_create_mission_rejects_non_string_subtasks(tmp_path):
    store = MissionStore(str(tmp_path))
    with pytest.raises(ValueError):
        create_mission_from_subtasks(
            mission_store=store, owner="admin",
            task_id="x", title="t", goal="g",
            subtasks=["ok", "", "alsoOk"],  # blank in middle
        )


def test_create_mission_rejects_non_list_input(tmp_path):
    store = MissionStore(str(tmp_path))
    with pytest.raises(TypeError):
        create_mission_from_subtasks(
            mission_store=store, owner="admin",
            task_id="x", title="t", goal="g",
            subtasks="not a list",  # type: ignore[arg-type]
        )


def test_link_existing_mission_returns_none_on_unknown_id(tmp_path):
    store = MissionStore(str(tmp_path))
    assert link_existing_mission_to_task(
        mission_store=store, mission_id="nonexistent", task_id="t1",
    ) is None


def test_link_existing_mission_is_idempotent(tmp_path):
    store = MissionStore(str(tmp_path))
    mission = create_mission_from_subtasks(
        mission_store=store, owner="admin",
        task_id="t1", title="t", goal="g",
        subtasks=["step A"],
    )
    # First link of same task — no-op
    link_existing_mission_to_task(
        mission_store=store, mission_id=mission.id, task_id="t1",
    )
    reloaded = store.get(mission.id)
    assert reloaded.metadata[META_MISSION_TASK_IDS] == ["t1"]
    # Add a NEW task
    link_existing_mission_to_task(
        mission_store=store, mission_id=mission.id, task_id="t2",
    )
    reloaded = store.get(mission.id)
    assert reloaded.metadata[META_MISSION_TASK_IDS] == ["t1", "t2"]


def test_mission_summary_counts_every_status_bucket(tmp_path):
    store = MissionStore(str(tmp_path))
    mission = create_mission_from_subtasks(
        mission_store=store, owner="admin",
        task_id="x", title="t", goal="g",
        subtasks=["step A", "step B", "step C", "step D"],
    )
    # Manually set various statuses (avoid going through MissionRunner)
    mission.steps[0].status = "done"
    mission.steps[1].status = "active"
    mission.steps[2].status = "blocked"
    # leave [3] as TODO
    summary = mission_summary(mission)
    assert summary["total_steps"] == 4
    assert summary["done"] == 1
    assert summary["in_progress"] == 1
    assert summary["blocked"] == 1
    assert summary["todo"] == 1


def test_mission_summary_next_actionable_is_first_runnable_todo(tmp_path):
    store = MissionStore(str(tmp_path))
    mission = create_mission_from_subtasks(
        mission_store=store, owner="admin",
        task_id="x", title="t", goal="g",
        subtasks=["step A", "step B", "step C"],
    )
    mission.steps[0].status = "done"
    # step B is TODO with no deps → should be next_actionable
    summary = mission_summary(mission)
    assert summary["next_actionable"] == "step B"


def test_enrich_task_without_mission_id_is_noop(tmp_path):
    store = MissionStore(str(tmp_path))
    task = {"id": "t", "metadata": {"other": "data"}}
    enrich_task_with_mission(task, store)
    assert task["metadata"] == {"other": "data"}
    assert META_TASK_MISSION_SUMMARY not in task["metadata"]


def test_enrich_task_with_broken_link_surfaces_none(tmp_path):
    """If the Mission was deleted, the consumer must be able to tell."""
    store = MissionStore(str(tmp_path))
    task = {
        "id": "t",
        "metadata": {META_TASK_MISSION_ID: "nonexistent-mission-id"},
    }
    enrich_task_with_mission(task, store)
    assert task["metadata"][META_TASK_MISSION_SUMMARY] is None


def test_enrich_task_with_real_mission_adds_summary(tmp_path):
    store = MissionStore(str(tmp_path))
    mission = create_mission_from_subtasks(
        mission_store=store, owner="admin",
        task_id="t1", title="Test mission", goal="g",
        subtasks=["work A", "work B"],
    )
    task = {
        "id": "t1",
        "metadata": {META_TASK_MISSION_ID: mission.id},
    }
    enrich_task_with_mission(task, store)
    summary = task["metadata"][META_TASK_MISSION_SUMMARY]
    assert summary["total_steps"] == 2
    assert task["metadata"][META_TASK_MISSION_STATUS] == mission.status


# ─── A2A RPC tasks/split end-to-end ──────────────────────────────────


pytestmark_crypto = pytest.mark.skipif(
    not crypto_available(),
    reason="A2A RPC integration needs PyNaCl for receipt signing",
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    return TestClient(create_app(tmp_path, require_console_auth=False))


@pytestmark_crypto
def test_tasks_split_creates_mission_and_links_task(client):
    # 1) Create a Task
    task = client.post(
        "/api/a2a/rpc",
        json=_rpc("message/send", {"message": _user_msg()}),
    ).json()["result"]
    task_id = task["id"]
    assert META_TASK_MISSION_ID not in task["metadata"]

    # 2) Split it
    split = client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {
            "id": task_id,
            "subtasks": ["draft", "review", "publish"],
            "title": "Launch announcement",
        }),
    ).json()
    assert "result" in split, split
    result = split["result"]

    # 3) Task now references the Mission
    mission_id = result["metadata"][META_TASK_MISSION_ID]
    assert mission_id
    summary = result["metadata"][META_TASK_MISSION_SUMMARY]
    assert summary["total_steps"] == 3
    assert summary["next_actionable"] == "draft"


@pytestmark_crypto
def test_tasks_split_rejects_empty_subtasks(client):
    task = client.post(
        "/api/a2a/rpc",
        json=_rpc("message/send", {"message": _user_msg()}),
    ).json()["result"]
    body = client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {"id": task["id"], "subtasks": []}),
    ).json()
    assert body["error"]["code"] == JSONRPC_INVALID_PARAMS


@pytestmark_crypto
def test_tasks_split_rejects_unknown_task(client):
    body = client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {
            "id": "nonexistent-task-id",
            "subtasks": ["x"],
        }),
    ).json()
    assert body["error"]["code"] == A2A_TASK_NOT_FOUND


@pytestmark_crypto
def test_tasks_split_rejects_already_linked_task(client):
    """注意力集中 contract: splitting twice would invalidate the
    progress assumptions the original consumer has. Force the
    consumer to consciously deal with the existing Mission."""
    task = client.post(
        "/api/a2a/rpc",
        json=_rpc("message/send", {"message": _user_msg()}),
    ).json()["result"]
    # First split — OK
    client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {
            "id": task["id"], "subtasks": ["a", "b"],
        }),
    )
    # Second split — rejected
    body = client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {
            "id": task["id"], "subtasks": ["c", "d"],
        }),
    ).json()
    assert body["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "already linked" in body["error"]["message"]


@pytestmark_crypto
def test_tasks_get_after_split_shows_mission_progress(client):
    task = client.post(
        "/api/a2a/rpc",
        json=_rpc("message/send", {"message": _user_msg()}),
    ).json()["result"]
    client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/split", {
            "id": task["id"], "subtasks": ["plan", "execute"],
        }),
    )
    fetched = client.post(
        "/api/a2a/rpc",
        json=_rpc("tasks/get", {"id": task["id"]}),
    ).json()["result"]
    summary = fetched["metadata"][META_TASK_MISSION_SUMMARY]
    assert summary["total_steps"] == 2
    assert summary["next_actionable"] == "plan"


@pytestmark_crypto
def test_tasks_split_appears_in_methods_list_on_unknown_method(client):
    """The unknown-method error data lists supported methods —
    confirm tasks/split is advertised so consumers can discover it."""
    body = client.post(
        "/api/a2a/rpc",
        json=_rpc("method/never_existed", {}),
    ).json()
    assert "tasks/split" in body["error"]["data"]["supported_methods"]
