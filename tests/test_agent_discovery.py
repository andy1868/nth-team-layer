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
    """Registry with 5 agents for complement testing."""
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
    # Carol: has python/web, no seeking, accepting tasks for code_review
    r3 = AgentRegistry(agents_dir=str(agents_dir))
    r3.register("carol", capabilities=["python", "web"], seeking=[],
                accepting_tasks=True, available_for=["code_review"],
                start_heartbeat=False)
    # Dave: has deploy, seeks nothing
    r4 = AgentRegistry(agents_dir=str(agents_dir))
    r4.register("dave", capabilities=["deploy"], seeking=[],
                start_heartbeat=False)
    # Eve: has solidity+audit, accepting tasks for audit only
    r5 = AgentRegistry(agents_dir=str(agents_dir))
    r5.register("eve", capabilities=["solidity", "audit"], seeking=[],
                accepting_tasks=True, available_for=["audit"],
                start_heartbeat=False)

    yield reg
    for r in [r1, r2, r3, r4, r5]:
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

    # ── H3: short() shows new fields ──

    def test_short_shows_seeking(self):
        r = AgentRecord(agent_id="alice", hostname="h", pid=1,
                        capabilities=["py"], seeking=["solidity"])
        s = r.short()
        assert "seek=[solidity]" in s

    def test_short_shows_accepting(self):
        r = AgentRecord(agent_id="bob", hostname="h", pid=1,
                        accepting_tasks=True)
        s = r.short()
        assert "accept" in s


# ────────────────────────── find_complements ──────────────────────────


class TestFindComplements:
    def test_finds_complement(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        assert len(results) >= 1
        bob = [m for m in results if m.record.agent_id == "bob"]
        assert len(bob) == 1
        assert "solidity" in bob[0].matched_capabilities

    def test_bidirectional_complement(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob")
        alice = [m for m in results if m.record.agent_id == "alice"]
        assert len(alice) == 1
        assert "python" in alice[0].matched_capabilities

    def test_no_complements_when_no_seeking(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        assert finder.find_complements("dave") == []

    def test_accepting_tasks_bonus(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        if len(results) >= 2:
            bob_score = [m.score for m in results if m.record.agent_id == "bob"][0]
            assert bob_score >= 1.0

    def test_unknown_agent(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        assert finder.find_complements("nobody") == []

    # ── H1: available_for filtering ──

    def test_available_for_filters(self, multi_agent_registry):
        """Eve accepts tasks for 'audit' only. Alice seeks 'solidity'.
        Eve has solidity, but not in available_for → no match for solidity."""
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        eve = [m for m in results if m.record.agent_id == "eve"]
        assert len(eve) == 0  # solidity not in eve's available_for

    def test_available_for_allows_match(self, multi_agent_registry):
        """Bob seeks python. Carol has python and available_for=['code_review'].
        'python' is not in available_for → should NOT match."""
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob")
        carol = [m for m in results if m.record.agent_id == "carol"]
        assert len(carol) == 0

    # ── M2: match_direction ──

    def test_match_direction(self, multi_agent_registry):
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        bob = [m for m in results if m.record.agent_id == "bob"][0]
        kind = bob.match_details
        assert "they_have" in kind
        assert "solidity" in kind["they_have"]
        assert "i_have" in kind
        assert "python" in kind["i_have"]


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


# ────────────────────────── M1: broadcast_order ──────────────────────────


class TestBroadcastOrder:
    def test_broadcast_creates_order(self, tmp_workspace):
        from nth_dao.marketplace import TaskMarketplace
        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-agent",
            marketplace_dir="test_orders",
        )
        order = mkt.broadcast_order("review PR", reward=10)
        assert order.title == "review PR"
        assert order.reward == 10
        assert order.status.value == "open"

    def test_broadcast_without_finder_still_creates(self, tmp_workspace):
        from nth_dao.marketplace import TaskMarketplace
        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-agent",
            marketplace_dir="test_orders",
        )
        order = mkt.broadcast_order("review PR")
        assert order is not None
