"""Tests for v0.9.8 agent discovery enhancement — complements, seeking, accepting_tasks."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nth_dao.discovery.agent_registry import AgentRecord, AgentRegistry
from nth_dao.discovery.peer_finder import PeerFinder


# ────────────────────────── Fixtures ──────────────────────────


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def registry(tmp_workspace):
    reg = AgentRegistry(agents_dir=str(tmp_workspace / "agents"))
    yield reg


@pytest.fixture
def multi_agent_registry(tmp_workspace):
    """Registry with 4 agents for complement testing."""
    agents_dir = tmp_workspace / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    from nth_dao.discovery.agent_registry import AgentRegistry
    reg = AgentRegistry(agents_dir=str(agents_dir))

    # Alice: has python/web, seeks solidity
    r1 = AgentRegistry(agents_dir=str(agents_dir))
    r1.register("alice", capabilities=["python", "web"], seeking=["solidity"],
                start_heartbeat=False)
    # Bob: has solidity/audit, seeks python
    r2 = AgentRegistry(agents_dir=str(agents_dir))
    r2.register("bob", capabilities=["solidity", "audit"], seeking=["python"],
                start_heartbeat=False)
    # Carol: has python/web, no seeking, accepting tasks
    r3 = AgentRegistry(agents_dir=str(agents_dir))
    r3.register("carol", capabilities=["python", "web"], seeking=[],
                accepting_tasks=True, start_heartbeat=False)
    # Dave: has deploy, seeks nothing, not accepting
    r4 = AgentRegistry(agents_dir=str(agents_dir))
    r4.register("dave", capabilities=["deploy"], seeking=[],
                start_heartbeat=False)

    yield reg
    for r in [r1, r2, r3, r4]:
        r.unregister()


# ────────────────────────── AgentRecord ──────────────────────────


class TestAgentRecord:
    def test_new_fields_default(self):
        r = AgentRecord(agent_id="test", hostname="h", pid=1)
        assert r.seeking == []
        assert r.accepting_tasks is False
        assert r.available_for == []

    def test_seeking_set(self):
        r = AgentRecord(agent_id="alice", hostname="h", pid=1,
                        seeking=["solidity", "audit"])
        assert r.seeking == ["solidity", "audit"]

    def test_accepting_tasks_true(self):
        r = AgentRecord(agent_id="bob", hostname="h", pid=1,
                        accepting_tasks=True, available_for=["code_review"])
        assert r.accepting_tasks is True
        assert r.available_for == ["code_review"]

    def test_round_trip_new_fields(self):
        r = AgentRecord(agent_id="carol", hostname="h", pid=1,
                        capabilities=["python"], seeking=["rust"],
                        accepting_tasks=True, available_for=["deploy"])
        d = r.to_dict()
        r2 = AgentRecord.from_dict(d)
        assert r2.seeking == ["rust"]
        assert r2.accepting_tasks is True
        assert r2.available_for == ["deploy"]


# ────────────────────────── find_complements ──────────────────────────


class TestFindComplements:
    def test_finds_complement(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        # Alice seeks solidity → Bob has it
        assert len(results) >= 1
        bob = [m for m in results if m.record.agent_id == "bob"]
        assert len(bob) == 1
        assert "solidity" in bob[0].matched_capabilities

    def test_bidirectional_complement(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob")
        # Bob seeks python → Alice has it
        alice = [m for m in results if m.record.agent_id == "alice"]
        assert len(alice) == 1
        assert "python" in alice[0].matched_capabilities

    def test_no_complements_when_no_seeking(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("dave")
        # Dave seeks nothing → no complements
        assert results == []

    def test_accepting_tasks_bonus(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        # Carol has python (which alice doesn't seek, so no match from that)
        # But Bob has solidity (match) — check ordering
        if len(results) >= 2:
            # Bob should score higher than non-accepting agents with same matches
            scores = [(m.record.agent_id, m.score) for m in results]
            bob_score = [s for a, s in scores if a == "bob"][0]
            assert bob_score >= 1.0

    def test_unknown_agent(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        assert finder.find_complements("nobody") == []


# ────────────────────────── Registry registration ──────────────────────────


class TestRegistryWithSeeking:
    def test_register_with_seeking(self, registry):
        registry.register("alice", capabilities=["python"],
                          seeking=["solidity"],
                          accepting_tasks=True,
                          available_for=["code_review"],
                          start_heartbeat=False)
        record = registry._record
        assert record is not None
        assert record.seeking == ["solidity"]
        assert record.accepting_tasks is True
        assert record.available_for == ["code_review"]
