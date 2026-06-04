"""Tests for nth_dao.event_subscriptions — pub/sub on EventBus."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nth_dao.event_bus import EventBus, BusEvent
from nth_dao.event_subscriptions import SubscriptionManager, Subscription


# ────────────────────────── Fixtures ──────────────────────────


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def event_bus(tmp_workspace):
    return EventBus(workspace=tmp_workspace)


@pytest.fixture
def manager(event_bus, tmp_workspace):
    return SubscriptionManager(event_bus, workspace=tmp_workspace)


# ────────────────────────── Subscription dataclass ──────────────────────────


class TestSubscription:
    def test_round_trip(self):
        sub = Subscription(
            subscription_id="abc",
            pattern="group.*",
            subscriber_id="alice",
            cursor="evt-001",
            metadata={"tags": ["critical"]},
        )
        d = sub.to_dict()
        sub2 = Subscription.from_dict(d)
        assert sub2.subscription_id == "abc"
        assert sub2.pattern == "group.*"
        assert sub2.subscriber_id == "alice"
        assert sub2.cursor == "evt-001"
        assert sub2.metadata == {"tags": ["critical"]}

    def test_matches_exact(self):
        sub = Subscription(subscription_id="s1", pattern="group.message.posted", subscriber_id="")
        assert sub.matches("group.message.posted")
        assert not sub.matches("group.message.deleted")

    def test_matches_glob(self):
        sub = Subscription(subscription_id="s1", pattern="group.*", subscriber_id="")
        assert sub.matches("group.message.posted")
        assert sub.matches("group.channel.created")
        assert not sub.matches("mission.started")

    def test_matches_wildcard(self):
        sub = Subscription(subscription_id="s1", pattern="*", subscriber_id="")
        assert sub.matches("anything.at.all")
        assert sub.matches("")

    def test_matches_single_char(self):
        sub = Subscription(subscription_id="s1", pattern="group.?.posted", subscriber_id="")
        assert sub.matches("group.x.posted")
        assert not sub.matches("group.xx.posted")

    def test_matches_defaults(self):
        sub = Subscription(subscription_id="s1", pattern="*", subscriber_id="")
        assert sub.cursor == ""
        assert sub.metadata == {}


# ────────────────────────── Subscribe / Unsubscribe ──────────────────────────


class TestSubscribeUnsubscribe:
    def test_subscribe_returns_id(self, manager):
        sid = manager.subscribe("test.*", lambda e: None)
        assert sid
        assert len(sid) == 12

    def test_subscribe_and_list(self, manager):
        manager.subscribe("group.*", lambda e: None, subscriber_id="alice")
        manager.subscribe("mission.*", lambda e: None, subscriber_id="bob")
        all_subs = manager.list_subscriptions()
        assert len(all_subs) == 2

    def test_list_filter_by_subscriber(self, manager):
        manager.subscribe("group.*", lambda e: None, subscriber_id="alice")
        manager.subscribe("mission.*", lambda e: None, subscriber_id="bob")
        alice_subs = manager.list_subscriptions(subscriber_id="alice")
        assert len(alice_subs) == 1
        assert alice_subs[0].subscriber_id == "alice"

    def test_unsubscribe(self, manager):
        sid = manager.subscribe("test.*", lambda e: None)
        assert manager.unsubscribe(sid)
        assert len(manager.list_subscriptions()) == 0

    def test_unsubscribe_nonexistent(self, manager):
        assert not manager.unsubscribe("nope")

    def test_subscription_lookup(self, manager):
        sid = manager.subscribe("test.*", lambda e: None)
        sub = manager.subscription(sid)
        assert sub is not None
        assert sub.pattern == "test.*"
        assert manager.subscription("nope") is None

    def test_multiple_subscriptions_same_pattern(self, manager):
        called = []
        manager.subscribe("test.*", lambda e: called.append(("a", e.event_id)))
        manager.subscribe("test.*", lambda e: called.append(("b", e.event_id)))
        event_bus = manager._event_bus
        event_bus.emit("test.foo", {"x": 1})
        manager.poll()
        assert len(called) == 2


# ────────────────────────── Polling ──────────────────────────


class TestPolling:
    def test_poll_delivers_matching_events(self, manager, event_bus):
        received = []
        manager.subscribe("deploy.*", lambda e: received.append(e))
        event_bus.emit("deploy.started", {"env": "prod"})
        event_bus.emit("deploy.completed", {"env": "prod"})
        delivered = manager.poll()
        assert len(delivered) == 2
        assert len(received) == 2
        assert received[0].event_type == "deploy.started"
        assert received[1].event_type == "deploy.completed"

    def test_poll_only_delivers_new_events(self, manager, event_bus):
        received = []
        manager.subscribe("test.*", lambda e: received.append(e))
        event_bus.emit("test.first", {})
        manager.poll()
        assert len(received) == 1
        # Poll again — no new events
        delivered = manager.poll()
        assert len(delivered) == 0
        assert len(received) == 1  # not called again

    def test_poll_skips_non_matching(self, manager, event_bus):
        received = []
        manager.subscribe("deploy.*", lambda e: received.append(e))
        event_bus.emit("other.event", {})
        delivered = manager.poll()
        assert len(delivered) == 0
        assert len(received) == 0

    def test_poll_empty_when_no_subscriptions(self, manager, event_bus):
        event_bus.emit("test.foo", {})
        delivered = manager.poll()
        assert delivered == []

    def test_poll_cursor_advances(self, manager, event_bus):
        sid = manager.subscribe("test.*", lambda e: None)
        event_bus.emit("test.a", {})
        manager.poll()
        sub = manager.subscription(sid)
        assert sub.cursor  # cursor should be set

    def test_poll_handles_callback_exception(self, manager, event_bus):
        received = []
        manager.subscribe("test.*", lambda e: (_ for _ in ()).throw(ValueError("boom")))
        manager.subscribe("test.*", lambda e: received.append(e))
        event_bus.emit("test.foo", {})
        delivered = manager.poll()
        # Second callback should still fire
        assert len(received) == 1


# ────────────────────────── poll_for ──────────────────────────


class TestPollFor:
    def test_poll_for_only_delivers_to_subscriber(self, manager, event_bus):
        alice_received = []
        bob_received = []
        manager.subscribe("test.*", lambda e: alice_received.append(e), subscriber_id="alice")
        manager.subscribe("test.*", lambda e: bob_received.append(e), subscriber_id="bob")
        event_bus.emit("test.foo", {})
        manager.poll_for("alice")
        assert len(alice_received) == 1
        assert len(bob_received) == 0

    def test_poll_for_empty(self, manager, event_bus):
        assert manager.poll_for("nobody") == []


# ────────────────────────── Persistence ──────────────────────────


class TestPersistence:
    def test_subscriptions_survive_new_instance(self, tmp_workspace, event_bus):
        mgr1 = SubscriptionManager(event_bus, workspace=tmp_workspace)
        mgr1.subscribe("test.*", lambda e: None, subscriber_id="alice")

        mgr2 = SubscriptionManager(event_bus, workspace=tmp_workspace)
        subs = mgr2.list_subscriptions()
        assert len(subs) == 1
        assert subs[0].pattern == "test.*"

    def test_poll_for_cursor_persists(self, manager, event_bus):
        manager.subscribe("test.*", lambda e: None, subscriber_id="alice")
        event_bus.emit("test.a", {})
        manager.poll_for("alice")
        # Subscriptions should be persisted (cursors updated)
        state_file = manager._state_path()
        assert state_file.exists()


# ────────────────────────── Edge cases ──────────────────────────


class TestEdgeCases:
    def test_repr(self, manager):
        r = repr(manager)
        assert "SubscriptionManager" in r
        assert "subscriptions=0" in r

    def test_repr_with_subs(self, manager):
        manager.subscribe("test.*", lambda e: None)
        r = repr(manager)
        assert "subscriptions=1" in r
