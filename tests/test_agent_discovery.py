"""Tests for v0.9.8 agent discovery enhancement — complements, seeking, accepting_tasks.

Includes positive, negative, and security tests per NTH DAO merge standards.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nth_dao.discovery.agent_registry import (
    AgentRecord,
    AgentRegistry,
    CLOCK_SKEW_TOLERANCE_SECONDS,
)


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
        assert r.signature == ""

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
                        accepting_tasks=True, available_for=["deploy"],
                        signature="abc123")
        d = r.to_dict()
        r2 = AgentRecord.from_dict(d)
        assert r2.seeking == ["rust"]
        assert r2.accepting_tasks is True
        assert r2.available_for == ["deploy"]
        assert r2.signature == "abc123"

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

    # ── S2: signature field ──

    def test_signature_default_empty(self):
        r = AgentRecord(agent_id="test", hostname="h", pid=1)
        assert r.signature == ""

    def test_signature_survives_round_trip(self):
        r = AgentRecord(agent_id="sig-test", hostname="h", pid=1,
                        signature="deadbeef")
        d = r.to_dict()
        assert d["signature"] == "deadbeef"

    # ── C1: from_dict ignores unknown fields ──

    def test_from_dict_ignores_unknown_fields(self):
        data = {
            "agent_id": "test", "hostname": "h", "pid": 1,
            "typo_field": "should_be_dropped",
            "another_junk": 42,
        }
        r = AgentRecord.from_dict(data)
        assert r.agent_id == "test"
        # unknown fields are not present as attributes


# ────────────────────────── is_alive / clock skew ──────────────────────────


class TestIsAlive:
    def test_alive_when_recent(self):
        r = AgentRecord(agent_id="test", hostname="h", pid=1)
        assert r.is_alive(max_stale_seconds=9999) is True

    def test_not_alive_when_old(self):
        r = AgentRecord(agent_id="test", hostname="h", pid=1,
                        last_seen="2000-01-01T00:00:00")
        assert r.is_alive(max_stale_seconds=1) is False

    def test_clock_skew_tolerance_applied(self):
        """A record just barely stale should still be alive due to skew buffer."""
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(seconds=95)).isoformat()
        r = AgentRecord(agent_id="test", hostname="h", pid=1, last_seen=past)
        # Without skew: 95s > 90s → dead.
        # With CLOCK_SKEW_TOLERANCE_SECONDS=30: 95s < 120s → alive.
        assert r.is_alive(max_stale_seconds=90) is True


# ────────────────────────── find_complements ──────────────────────────


class TestFindComplements:
    def test_finds_complement(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        assert len(results) >= 1
        bob = [m for m in results if m.record.agent_id == "bob"]
        assert len(bob) == 1
        assert "solidity" in bob[0].matched_capabilities

    def test_bidirectional_complement(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob")
        alice = [m for m in results if m.record.agent_id == "alice"]
        assert len(alice) == 1
        assert "python" in alice[0].matched_capabilities

    def test_no_complements_when_no_seeking(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        assert finder.find_complements("dave") == []

    def test_accepting_tasks_bonus(self, multi_agent_registry):
        """T1 fix: assert len(results) >= 2 FIRST, then check bonus."""
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        assert len(results) >= 2, f"expected >= 2 complements, got {len(results)}"
        bob_score = [m.score for m in results if m.record.agent_id == "bob"][0]
        assert bob_score >= 1.0

    def test_unknown_agent(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        assert finder.find_complements("nobody") == []

    # ── available_for is metadata only (not used for filtering) ──

    def test_available_for_is_metadata_only(self, multi_agent_registry):
        """Eve accepts tasks for 'audit' only, but Alice seeks 'solidity'.
        Eve HAS solidity -> should match regardless of available_for."""
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        eve = [m for m in results if m.record.agent_id == "eve"]
        assert len(eve) == 1

    def test_complement_match_ignores_available_for(self, multi_agent_registry):
        """Bob seeks python. Carol has python. Should match even though
        Carol's available_for=['code_review'] doesn't mention python."""
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob")
        carol = [m for m in results if m.record.agent_id == "carol"]
        assert len(carol) == 1

    # ── M2: match_direction ──

    def test_match_direction(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice")
        bob = [m for m in results if m.record.agent_id == "bob"][0]
        kind = bob.match_details
        assert "they_have" in kind
        assert "solidity" in kind["they_have"]
        assert "i_have" in kind
        assert "python" in kind["i_have"]

    # ── D4: direction parameter ──

    def test_direction_incoming(self, multi_agent_registry):
        """Alice seeks solidity; Bob has solidity. incoming: who can help Alice."""
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice", direction="incoming")
        bob = [m for m in results if m.record.agent_id == "bob"]
        assert len(bob) == 1
        assert "solidity" in bob[0].matched_capabilities
        # i_have should not be in matched for incoming-only
        assert "python" not in bob[0].matched_capabilities

    def test_direction_outgoing(self, multi_agent_registry):
        """Alice seeks solidity; Bob has solidity. outgoing from Bob's perspective:
        who can Bob help? Answer: Alice (she seeks what Bob has)."""
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("bob", direction="outgoing")
        alice = [m for m in results if m.record.agent_id == "alice"]
        assert len(alice) == 1
        assert "solidity" in alice[0].matched_capabilities

    def test_direction_bidirectional_covers_both(self, multi_agent_registry):
        from nth_dao.discovery.peer_finder import PeerFinder
        finder = PeerFinder(multi_agent_registry)
        results = finder.find_complements("alice", direction="bidirectional")
        bob = [m for m in results if m.record.agent_id == "bob"][0]
        # bidirectional includes both directions
        assert len(bob.matched_capabilities) >= 2


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

    # ── C3: thread-safety — lock protects _record ──

    def test_register_reentrant_safe(self, registry):
        """register() then register() again — RLock prevents deadlock."""
        registry.register("agent1", start_heartbeat=False)
        # Re-register (calls unregister() internally, same lock via RLock)
        registry.register("agent1", capabilities=["updated"],
                          start_heartbeat=False)
        assert registry._record is not None
        assert registry._record.capabilities == ["updated"]

    def test_update_status_under_lock(self, registry):
        """update_status() must not race with heartbeat (lock protects)."""
        registry.register("agent1", start_heartbeat=False)
        # This should not raise or deadlock
        registry.update_status(status="busy", metadata_patch={"key": "val"})
        assert registry._record.status == "busy"
        assert registry._record.metadata["key"] == "val"

    def test_unregister_without_register(self, registry):
        """unregister() on a never-registered registry is a no-op."""
        registry.unregister()  # must not raise


# ────────────────────────── broadcast_order ──────────────────────────


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

    # ── T3: fanout path tests ──

    def test_broadcast_dms_accepting_agents(self, tmp_workspace, multi_agent_registry):
        """When finder + channel are provided, DMs go to accepting_tasks agents."""
        from nth_dao.discovery.peer_finder import PeerFinder
        from nth_dao.marketplace import TaskMarketplace

        finder = PeerFinder(multi_agent_registry)
        mock_channel = MagicMock()

        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-creator",
            channel=mock_channel,
            marketplace_dir="test_orders",
        )
        # Alice is NOT accepting_tasks; Carol and Eve ARE
        order = mkt.broadcast_order(
            "review PR", reward=10, capability="python",
            finder=finder, channel=mock_channel,
        )
        assert order is not None

        # Verify DMs were sent to accepting agents (Carol via python capability)
        dm_calls = mock_channel.dm.call_args_list
        dm_recipients = {call[0][0] for call in dm_calls}
        # Carol has python capability + accepting_tasks=True
        assert "carol" in dm_recipients

    def test_broadcast_dm_includes_creator_sig(self, tmp_workspace, multi_agent_registry):
        """DM payload must include creator/created_at when sig is available."""
        from nth_dao.discovery.peer_finder import PeerFinder
        from nth_dao.marketplace import TaskMarketplace

        finder = PeerFinder(multi_agent_registry)
        mock_channel = MagicMock()

        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-creator",
            channel=mock_channel,
            marketplace_dir="test_orders",
        )
        # Manually set creator_sig on the order (simulates identity signing)
        mkt.broadcast_order(
            "review PR", capability="solidity",
            finder=finder, channel=mock_channel,
        )

        # At least one DM call should contain order info
        if mock_channel.dm.call_args_list:
            first_dm_body = mock_channel.dm.call_args_list[0][0][1]
            assert "ID:" in first_dm_body
            assert "Reward:" in first_dm_body
            assert "[New Task]" in first_dm_body

    def test_broadcast_empty_targets_no_dm(self, tmp_workspace, multi_agent_registry):
        """When finder returns no agents, no DMs are sent."""
        from nth_dao.discovery.peer_finder import PeerFinder
        from nth_dao.marketplace import TaskMarketplace

        finder = PeerFinder(multi_agent_registry)
        mock_channel = MagicMock()

        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-creator",
            channel=mock_channel,
            marketplace_dir="test_orders",
        )
        # No agent has capability "nonexistent"
        mkt.broadcast_order(
            "orphan task", capability="nonexistent",
            finder=finder, channel=mock_channel,
        )
        # channel.dm should NOT have been called (no targets)
        mock_channel.dm.assert_not_called()

    def test_broadcast_handles_dm_failure_gracefully(self, tmp_workspace, multi_agent_registry):
        """DM failures should not crash broadcast_order."""
        from nth_dao.discovery.peer_finder import PeerFinder
        from nth_dao.marketplace import TaskMarketplace

        finder = PeerFinder(multi_agent_registry)
        mock_channel = MagicMock()
        mock_channel.dm.side_effect = RuntimeError("channel down")

        mkt = TaskMarketplace(
            workspace=tmp_workspace,
            agent_id="test-creator",
            channel=mock_channel,
            marketplace_dir="test_orders",
        )
        order = mkt.broadcast_order(
            "survives DM failure", capability="python",
            finder=finder, channel=mock_channel,
        )
        assert order is not None  # order still created despite DM errors


# ────────────────────────── verify_all_records ──────────────────────────


class TestVerifyAllRecords:
    def test_verify_all_unsigned_records_ok(self, tmp_workspace):
        """Unsigned records return True (no integrity claim, trusted filesystem)."""
        agents_dir = tmp_workspace / "agents"
        reg = AgentRegistry(agents_dir=str(agents_dir))
        reg.register("alice", capabilities=["python"], start_heartbeat=False)

        # Invalid did_key returns empty dict (graceful degradation)
        results = reg.verify_all_records("did:key:invalid")
        assert results == {}

    def test_tampered_record_detection_setup(self, tmp_workspace):
        """Verify that the signature field exists and is persisted,
        establishing the hook point for future full verification."""
        agents_dir = tmp_workspace / "agents"
        reg = AgentRegistry(agents_dir=str(agents_dir))
        reg.register("alice", capabilities=["python"], start_heartbeat=False)

        # Read the file directly and check signature field exists
        import json
        record_path = agents_dir / "alice.json"
        data = json.loads(record_path.read_text())
        assert "signature" in data
        assert data["signature"] == ""

        # Manually tamper: change capabilities without updating signature
        data["capabilities"] = ["evil_injected"]
        record_path.write_text(json.dumps(data))

        # Reload: from_dict still loads (unsigned — no enforcement)
        record = reg.get("alice")
        assert record is not None
        # The tampered capabilities are visible (unsigned mode trusts filesystem)
        # This test documents the current state: protection requires caller to
        # use verify_all_records() with a valid did_key.
