"""SubscriptionManager — thin pub/sub layer on top of ``EventBus.replay()``.

Polling-based: callers invoke ``poll()`` (or a long-running loop in a
worker thread); each subscription tracks its own cursor in memory so
narrow subscriptions never stall on unrelated traffic, and a wildcard
subscription never advances past unrelated subscribers' positions.

Original submission by @andy1868 weighed 384 LOC with persistence,
shared "advance all cursors" semantics, and two non-mixable poll
modes. This rewrite is ~100 LOC, single API, per-subscription cursor,
and explicitly does NOT persist subscriptions (callbacks aren't
persistable anyway, so persisting their metadata creates a
correctness footgun on restart). To survive a process restart, a
caller re-subscribes after attach() and gets every event since
``start_from``.

Usage::

    subs = SubscriptionManager(event_bus=team.event_bus)
    sub_id = subs.subscribe(
        "group.message.*",
        callback=lambda ev: print(f"new msg: {ev.payload}"),
        subscriber_id="agent-alice",
    )
    n = subs.poll()         # fires callbacks for all matching new events
    subs.unsubscribe(sub_id)
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .event_bus import EventBus, BusEvent

logger = logging.getLogger("nth_dao.event_subscriptions")


@dataclass
class Subscription:
    """One in-memory subscription. Tracks its OWN cursor — narrow
    subscriptions never stall on unrelated traffic."""

    subscription_id: str
    pattern: str
    subscriber_id: str
    callback: Callable[["BusEvent"], Any] = field(repr=False)
    cursor: str = ""    # last delivered event_id; "" means "from start"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches(self, event_type: str) -> bool:
        return fnmatch.fnmatch(event_type, self.pattern)


class SubscriptionManager:
    """Per-subscription cursors over an EventBus."""

    def __init__(
        self,
        event_bus: "EventBus",
        *,
        max_deliveries_per_poll: int = 100,
    ):
        self._bus = event_bus
        self._max_deliveries = max(1, max_deliveries_per_poll)
        self._lock = threading.Lock()
        self._subs: Dict[str, Subscription] = {}

    # ── registry ───────────────────────────────────────────

    def subscribe(
        self,
        pattern: str,
        callback: Callable[["BusEvent"], Any],
        *,
        subscriber_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        start_from: str = "",
    ) -> str:
        """Register a subscription; return its id.

        ``pattern`` uses glob syntax (``group.*``, ``*.posted``, ``*``).
        ``start_from`` is the last-seen event_id to resume from; empty
        means "deliver every event from the start of the stream".
        """
        with self._lock:
            sub_id = uuid.uuid4().hex[:12]
            self._subs[sub_id] = Subscription(
                subscription_id=sub_id,
                pattern=pattern,
                subscriber_id=subscriber_id,
                callback=callback,
                cursor=start_from,
                metadata=dict(metadata or {}),
            )
            return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            return self._subs.pop(subscription_id, None) is not None

    def list_subscriptions(self, subscriber_id: str = "") -> List[Subscription]:
        with self._lock:
            subs = list(self._subs.values())
            if subscriber_id:
                subs = [s for s in subs if s.subscriber_id == subscriber_id]
            return sorted(subs, key=lambda s: s.created_at)

    def subscription(self, subscription_id: str) -> Optional[Subscription]:
        with self._lock:
            return self._subs.get(subscription_id)

    # ── delivery ───────────────────────────────────────────

    def poll(self) -> int:
        """Deliver new events to matching subscriptions.

        Returns the total number of deliveries. Per-subscription
        cursors are advanced independently so a narrow pattern never
        misses an event because a broader pattern raced ahead.
        """
        delivered = 0
        with self._lock:
            sub_ids = list(self._subs)
        for sub_id in sub_ids:
            delivered += self._deliver_one(sub_id)
        return delivered

    def _deliver_one(self, sub_id: str) -> int:
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None:
                return 0
            cursor = sub.cursor
            pattern = sub.pattern
            callback = sub.callback
        n = 0
        last_event_id = cursor
        # replay(from_id=cursor) yields events AFTER `cursor`. When cursor
        # is "" we get the whole stream from the start, which is what a
        # fresh subscription wants.
        for event in self._bus.replay(from_id=cursor or None):
            if n >= self._max_deliveries:
                break
            if not fnmatch.fnmatch(event.event_type, pattern):
                last_event_id = event.event_id    # advance even if skipped
                continue
            try:
                callback(event)
            except Exception as exc:   # noqa: BLE001
                # A misbehaving subscriber MUST NOT freeze the others.
                logger.warning("subscription %s callback failed: %s", sub_id, exc)
            last_event_id = event.event_id
            n += 1
        # Persist the advanced cursor under the lock so two pollers
        # don't both replay the same window.
        if last_event_id != cursor:
            with self._lock:
                sub = self._subs.get(sub_id)
                if sub is not None:
                    sub.cursor = last_event_id
        return n


__all__ = ["Subscription", "SubscriptionManager"]
