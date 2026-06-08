"""A2A Task ↔ NTH Mission bridge — L1-4 (2026-06-08).

Strategic role: this is the "任务分割" engine. An incoming A2A
``message/send`` is a single Task at the wire level, but real work
breaks into many steps. NTH DAO already has Mission/MissionStep with
dependencies, assignees, acceptance criteria, audit trails, etc. The
bridge stitches the two so external A2A consumers see a stable Task
ID while NTH internally drives the work as a richer Mission graph.

Two directions:

  A2A Task → Mission (import)
    Used when an A2A consumer wants NTH to actually DO complex work.
    The consumer either:
      * sends ``message.metadata.mission_id`` referring to an
        existing Mission — bridge links the new Task to it
      * sends ``message.metadata.subtasks`` (list of strings) on a
        message/send for a NEW task — bridge creates a fresh Mission
        with those steps and links the Task to it
      * later uses the (separate) ``tasks/split`` RPC method to
        attach subtasks to an already-running Task

  Mission → A2A Task (export)
    Used by ``tasks/get`` to enrich the Task response with progress
    visibility. Adds ``metadata.mission`` carrying step count,
    status breakdown, next actionable step.

The bridge is intentionally one-way for state: A2A's message_history
does NOT mutate the Mission's step.output (that requires deliberate
NTH-side action through MissionRunner). The bridge enriches view
only, leaves authority where it belongs.

Linkage fields on the A2A Task ``metadata``:

  nth_mission_id        — string, set when Task is linked to a Mission
  nth_mission_status    — mirrored from Mission.status at view time
  nth_mission_step_summary  — {"total": N, "done": M, "next": "<desc>"}

Linkage on the Mission ``metadata``:

  a2a_task_ids          — list of Task IDs that surface this mission
                          (one Mission can be exposed via multiple
                          Tasks if multiple consumers subscribe)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from nth_dao.orchestration.mission import Mission
    from nth_dao.orchestration.mission_store import MissionStore

logger = logging.getLogger("nth_dao.a2a_mission_bridge")


# Metadata keys (kept as constants to prevent typo drift)
META_TASK_MISSION_ID = "nth_mission_id"
META_TASK_MISSION_STATUS = "nth_mission_status"
META_TASK_MISSION_SUMMARY = "nth_mission_step_summary"
META_MISSION_TASK_IDS = "a2a_task_ids"


# ─── A2A Task → Mission ─────────────────────────────────────────────


def create_mission_from_subtasks(
    *,
    mission_store: "MissionStore",
    owner: str,
    task_id: str,
    title: str,
    goal: str,
    subtasks: List[str],
) -> "Mission":
    """Materialise a fresh Mission from a list of subtask description
    strings supplied by an A2A consumer.

    Each string becomes one ``MissionStep`` with default status TODO
    and no dependencies. Order is preserved (insertion order = step
    order in the mission); the consumer chooses ordering by ordering
    the input list. Cross-step dependencies can be added later via
    the orchestration API — at v1 the bridge keeps the structure
    flat.

    The new Mission carries ``metadata.a2a_task_ids = [task_id]`` so
    NTH-side tooling can find which A2A Tasks observe this mission.

    Args:
        mission_store: the workspace MissionStore.
        owner: the NTH-side owner agent_id (typically the bootstrap
            admin for v1; future revisions may extract from the
            cap_token's subject_did when delegated).
        task_id: the A2A Task ID being linked.
        title: human-readable mission title.
        goal: the higher-level goal string.
        subtasks: list of step descriptions. Empty list rejected
            (use a plain Task without a Mission instead).

    Returns:
        The persisted ``Mission`` object.

    Raises:
        ValueError if subtasks is empty.
    """
    # Local import — Mission depends on identity/canonical_json chain
    # we don't want to drag at module-load time when the bridge is
    # imported by the web layer.
    from nth_dao.orchestration.mission import Mission

    if not subtasks:
        raise ValueError(
            "subtasks must be non-empty; pass [] and the caller "
            "should NOT use the bridge — a flat Task is the right "
            "shape"
        )
    if not isinstance(subtasks, list):
        raise TypeError(
            f"subtasks must be a list, got {type(subtasks).__name__}"
        )
    for i, s in enumerate(subtasks):
        if not isinstance(s, str) or not s.strip():
            raise ValueError(
                f"subtasks[{i}] must be a non-empty string"
            )

    steps_payload = [
        {"description": s.strip()} for s in subtasks
    ]
    mission = Mission.new(
        title=title,
        goal=goal,
        owner=owner,
        steps=steps_payload,
    )
    mission.metadata = dict(mission.metadata or {})
    mission.metadata[META_MISSION_TASK_IDS] = [task_id]
    mission_store.create(mission)
    return mission


def link_existing_mission_to_task(
    *,
    mission_store: "MissionStore",
    mission_id: str,
    task_id: str,
) -> Optional["Mission"]:
    """Add ``task_id`` to an existing Mission's ``a2a_task_ids`` list.

    Returns the updated Mission, or None if the mission_id is
    unknown. Idempotent — adding the same task_id twice is a no-op.
    """
    mission = mission_store.get(mission_id)
    if mission is None:
        return None
    metadata = dict(mission.metadata or {})
    task_ids = list(metadata.get(META_MISSION_TASK_IDS, []) or [])
    if task_id and task_id not in task_ids:
        task_ids.append(task_id)
        metadata[META_MISSION_TASK_IDS] = task_ids
        mission.metadata = metadata
        mission_store.save(mission)
    return mission


# ─── Mission → A2A Task (view enrichment) ───────────────────────────


def mission_summary(mission: "Mission") -> Dict[str, Any]:
    """Compact summary suitable for embedding in A2A Task metadata.

    Includes: total steps, terminal-state count (done + failed +
    handed_off), next actionable step description, and the mission's
    own status. An A2A consumer reading ``tasks/get`` then knows
    roughly where the work is without needing a second roundtrip
    into NTH's mission API.
    """
    steps = list(mission.steps or [])
    total = len(steps)
    done = sum(1 for s in steps if s.status == "done")
    failed = sum(1 for s in steps if s.status == "failed")
    in_progress = sum(
        1 for s in steps
        if s.status in ("claimed", "active", "needs_review")
    )
    todo = sum(1 for s in steps if s.status == "todo")
    blocked = sum(1 for s in steps if s.status == "blocked")

    # First actionable step is the first TODO with all deps done.
    completed_ids = {s.id for s in steps if s.status == "done"}
    next_desc = ""
    for s in steps:
        if s.status == "todo" and s.can_start(completed_ids):
            next_desc = s.description
            break

    return {
        "mission_id": mission.id,
        "title": mission.title,
        "status": mission.status,
        "total_steps": total,
        "done": done,
        "failed": failed,
        "in_progress": in_progress,
        "todo": todo,
        "blocked": blocked,
        "next_actionable": next_desc,
    }


def enrich_task_with_mission(
    task: Dict[str, Any],
    mission_store: "MissionStore",
) -> Dict[str, Any]:
    """If the Task's metadata references a Mission, attach a fresh
    summary on the response shape.

    Mutates and returns the input ``task`` dict (callers typically
    operate on the result anyway). If the linkage is broken — e.g.
    mission_id points to a deleted mission — the summary field is
    set to None so the consumer can detect the stale link.
    """
    metadata = task.get("metadata") or {}
    mission_id = str(metadata.get(META_TASK_MISSION_ID, "") or "")
    if not mission_id:
        return task
    mission = mission_store.get(mission_id)
    if mission is None:
        # Broken link — surface it honestly
        metadata.setdefault(META_TASK_MISSION_STATUS, "")
        metadata[META_TASK_MISSION_SUMMARY] = None
        task["metadata"] = metadata
        return task
    metadata[META_TASK_MISSION_STATUS] = mission.status
    metadata[META_TASK_MISSION_SUMMARY] = mission_summary(mission)
    task["metadata"] = metadata
    return task
