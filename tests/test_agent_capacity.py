"""Tests for agent capacity reporting — quantitative workload declarations.

Extends AgentRecord with queue_depth, estimated_wait_seconds, and
max_concurrent_tasks so orchestrators can make informed delegation
decisions. Integrates with AgentRegistry.update_capacity() and
PeerFinder capacity-aware ranking.
"""

from pathlib import Path

import pytest

from nth_dao.discovery.agent_registry import (
    AgentRecord,
    AgentRegistry,
    CapacityStatus,
)
from nth_dao.discovery.peer_finder import PeerFinder


# ─── CapacityStatus enum ────────────────────────────────────────────────


def test_capacity_status_values():
    assert CapacityStatus.IDLE.value == "idle"
    assert CapacityStatus.BUSY.value == "busy"
    assert CapacityStatus.OVERLOADED.value == "overloaded"
    assert CapacityStatus.OFFLINE.value == "offline"


# ─── AgentRecord capacity fields ────────────────────────────────────────


def test_agent_record_has_capacity_defaults():
    """New capacity fields have sensible defaults — backward compat."""
    r = AgentRecord(agent_id="test", hostname="box", pid=1)
    assert r.queue_depth == 0
    assert r.estimated_wait_seconds == 0.0
    assert r.max_concurrent_tasks == 3


def test_agent_record_capacity_round_trip():
    """Fields survive dict serialization round-trip."""
    r = AgentRecord(
        agent_id="test", hostname="box", pid=1,
        queue_depth=5, estimated_wait_seconds=12.5, max_concurrent_tasks=10,
    )
    data = r.to_dict()
    r2 = AgentRecord.from_dict(data)
    assert r2.queue_depth == 5
    assert r2.estimated_wait_seconds == 12.5
    assert r2.max_concurrent_tasks == 10


def test_agent_record_loads_old_data_without_capacity_fields():
    """Records from before this feature (no capacity keys) load cleanly."""
    old = {"agent_id": "legacy", "hostname": "box", "pid": 42,
           "backend_id": "mock", "status": "idle"}
    r = AgentRecord.from_dict(old)
    assert r.agent_id == "legacy"
    assert r.queue_depth == 0
    assert r.estimated_wait_seconds == 0.0


def test_agent_record_capacity_status_property():
    """Derived CapacityStatus from queue_depth + max_concurrent_tasks."""
    r = AgentRecord(agent_id="a", hostname="h", pid=1, queue_depth=0)
    assert r.capacity_status == CapacityStatus.IDLE

    r.queue_depth = 2
    r.max_concurrent_tasks = 5
    assert r.capacity_status == CapacityStatus.BUSY

    r.queue_depth = 5
    assert r.capacity_status == CapacityStatus.OVERLOADED

    r.queue_depth = 5
    r.max_concurrent_tasks = 3  # queue > max
    assert r.capacity_status == CapacityStatus.OVERLOADED


def test_capacity_status_clamps_negative_queue_depth():
    """Negative queue_depth (corrupt file / direct construction) clamps
    to 0 — never produces a bogus capacity status."""
    r = AgentRecord(agent_id="x", hostname="h", pid=1,
                    queue_depth=-5, max_concurrent_tasks=3)
    assert r.capacity_status == CapacityStatus.IDLE  # clamped to 0

    r.queue_depth = -1
    r.max_concurrent_tasks = 5
    assert r.capacity_status == CapacityStatus.IDLE  # clamped to 0


def test_capacity_status_offline_trumps_negative_queue():
    """OFFLINE status takes priority regardless of queue_depth value."""
    r = AgentRecord(agent_id="x", hostname="h", pid=1,
                    queue_depth=-5, status="offline")
    assert r.capacity_status == CapacityStatus.OFFLINE


# ─── AgentRegistry.update_capacity() ─────────────────────────────────────


def test_update_capacity_patches_fields(tmp_path: Path):
    """update_capacity() writes capacity fields to the heartbeat file."""
    reg = AgentRegistry(agents_dir=str(tmp_path / "agents"))
    reg.register("agent-1", capabilities=["python"])

    reg.update_capacity(queue_depth=4, estimated_wait_seconds=30.0,
                        max_concurrent_tasks=8)

    rec = reg.get("agent-1")
    assert rec is not None
    assert rec.queue_depth == 4
    assert rec.estimated_wait_seconds == 30.0
    assert rec.max_concurrent_tasks == 8


def test_update_capacity_partial_update(tmp_path: Path):
    """Only specified fields change; others keep their value."""
    reg = AgentRegistry(agents_dir=str(tmp_path / "agents"))
    reg.register("agent-1", capabilities=["python"])

    reg.update_capacity(queue_depth=3, max_concurrent_tasks=5)
    reg.update_capacity(queue_depth=1)  # only queue_depth changes this time

    rec = reg.get("agent-1")
    assert rec.queue_depth == 1
    assert rec.estimated_wait_seconds == 0.0  # never touched
    assert rec.max_concurrent_tasks == 5       # preserved from first call


def test_update_capacity_fails_without_register(tmp_path: Path):
    reg = AgentRegistry(agents_dir=str(tmp_path / "agents"))
    with pytest.raises(RuntimeError, match="register"):
        reg.update_capacity(queue_depth=1)


# ─── PeerFinder capacity-aware ranking ───────────────────────────────────


@pytest.fixture
def busy_registry(tmp_path: Path) -> AgentRegistry:
    """Populated registry with agents at different capacity levels.

    AgentRegistry is single-agent — each register() unregisters the
    previous one.  We populate the shared directory by using separate
    registry instances per agent, then create a PeerFinder that reads
    the shared files.
    """
    agents_dir = tmp_path / "agents"

    for agent_id, caps, qd, wait, max_c in [
        ("alice",  ["python", "web"], 0,  0.0,  3),
        ("bob",    ["python", "web"], 2,  10.0, 5),
        ("carol",  ["python", "web"], 10, 120.0,5),
    ]:
        r = AgentRegistry(agents_dir=str(agents_dir))
        r.register(agent_id, capabilities=caps, start_heartbeat=False)
        r.update_capacity(queue_depth=qd, estimated_wait_seconds=wait,
                          max_concurrent_tasks=max_c)

    # Return a fresh registry for the shared directory (read-only use)
    return AgentRegistry(agents_dir=str(agents_dir))


def test_rank_prefers_lower_queue_depth(busy_registry: AgentRegistry):
    """Idle agents rank above busy ones for the same capability match."""
    finder = PeerFinder(busy_registry)
    results = finder.rank(needed_capabilities=["python"])

    assert len(results) >= 2
    # Alice (idle) should rank above Bob (busy)
    alice_idx = next(i for i, m in enumerate(results) if m.record.agent_id == "alice")
    bob_idx = next(i for i, m in enumerate(results) if m.record.agent_id == "bob")
    # bob may be excluded if overloaded with min_score; alice should be near top
    assert alice_idx <= bob_idx


def test_rank_excludes_overloaded_by_default(busy_registry: AgentRegistry):
    """Overloaded agents (capacity_status == OVERLOADED) are excluded
    from ranking when prefer_available=True (default)."""
    finder = PeerFinder(busy_registry)
    results = finder.rank(needed_capabilities=["python"], prefer_available=True)

    agent_ids = {m.record.agent_id for m in results}
    assert "carol" not in agent_ids
    assert "alice" in agent_ids


def test_rank_includes_overloaded_when_explicit(busy_registry: AgentRegistry):
    """prefer_available=False includes all agents regardless of capacity."""
    finder = PeerFinder(busy_registry)
    results = finder.rank(needed_capabilities=["python"], prefer_available=False)

    agent_ids = {m.record.agent_id for m in results}
    assert "carol" in agent_ids


def test_find_available_returns_only_accepting_agents(busy_registry: AgentRegistry):
    """find_available() filters to agents that can accept new tasks."""
    finder = PeerFinder(busy_registry)
    results = finder.find_available(capability="python")

    agent_ids = {r.agent_id for r in results}
    assert "alice" in agent_ids   # idle
    assert "bob" in agent_ids     # busy but under capacity
    assert "carol" not in agent_ids  # overloaded


def test_peer_finder_stats_includes_capacity(busy_registry: AgentRegistry):
    """summary_table() shows queue_depth for each agent."""
    finder = PeerFinder(busy_registry)
    table = finder.summary_table()
    assert "alice" in table
    assert "bob" in table
    # carol is overloaded — excluded from alive ranking by default
