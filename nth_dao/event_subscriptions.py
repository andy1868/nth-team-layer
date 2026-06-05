"""SubscriptionManager - thin pub/sub layer on top of ``EventBus.replay()``.

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
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

from .util import now_iso

if TYPE_CHECKING:
    from .event_bus import EventBus, BusEvent

logger = logging.getLogger("nth_dao.event_subscriptions")

# Characters that turn an fnmatch pattern into a glob (vs exact match).
# If a pattern has none of these, we can push it down into EventBus.replay's
# event_types filter and skip the full-stream scan (H-7 fast path).
_GLOB_META = set("*?[]")


@dataclass
class Subscription:
    """One in-memory subscription. Tracks its OWN cursor - narrow
    subscriptions never stall on unrelated traffic."""

    subscription_id: str
    pattern: str
    subscriber_id: str
    callback: Callable[["BusEvent"], Any] = field(repr=False)
    cursor: str = ""    # last delivered event_id; "" means "from start"
    created_at: str = field(default_factory=now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches(self, event_type: str) -> bool:
        # C-7 fix: fnmatch.fnmatch is case-insensitive on Windows but
        # case-sensitive on POSIX. Using fnmatchcase guarantees identical
        # behaviour on every host - critical for cross-platform NTH DAO.
        return fnmatch.fnmatchcase(event_type, self.pattern)


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
        # H-8 fix: per-subscription "currently polling" sentinel. Without
        # this, a release-then-reacquire window between reading cursor and
        # writing it back let a SECOND concurrent poll for the same sub
        # start from the stale cursor and double-deliver events.
        self._polling: Set[str] = set()

    # -- registry -------------------------------------------

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

    # -- delivery -------------------------------------------

    def poll(self) -> int:
        """Deliver new events to matching subscriptions.

        Returns the *total* number of deliveries across all
        subscriptions. NB: ``max_deliveries_per_poll`` caps EACH
        subscription independently; a single poll() with N matching
        subscriptions can deliver up to N x cap (M-1 clarification).

        Per-subscription cursors advance independently so a narrow
        pattern never stalls when a wildcard subscription races
        ahead. Concurrent poll() calls for the same subscription are
        serialised by a polling sentinel (H-8) so events are never
        double-delivered.
        """
        delivered = 0
        with self._lock:
            sub_ids = list(self._subs)
        for sub_id in sub_ids:
            delivered += self._deliver_one(sub_id)
        return delivered

    def _deliver_one(self, sub_id: str) -> int:
        # H-8 fix: claim the polling sentinel under the lock; if a
        # concurrent poll is already in flight for this sub, skip it
        # rather than starting a second walk from a stale cursor.
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None or sub_id in self._polling:
                return 0
            self._polling.add(sub_id)
            cursor = sub.cursor
            pattern = sub.pattern
            callback = sub.callback

        try:
            # H-7 fix: if the pattern has no glob meta-characters we know
            # it's an EXACT event_type and we can push the filter down
            # into the bus replay path - avoiding the per-event fnmatch
            # call AND letting any future replay-side optimisation cut
            # the scan early.
            event_types = None
            if not any(c in _GLOB_META for c in pattern):
                event_types = [pattern]

            n = 0
            last_event_id = cursor
            for event in self._bus.replay(
                from_id=cursor or None,
                event_types=event_types,
            ):
                if n >= self._max_deliveries:
                    break
                if event_types is None and not fnmatch.fnmatchcase(event.event_type, pattern):
                    last_event_id = event.event_id    # advance even if skipped
                    continue
                try:
                    callback(event)
                except Exception as exc:   # noqa: BLE001
                    # A misbehaving subscriber MUST NOT freeze the others.
                    logger.warning("subscription %s callback failed: %s", sub_id, exc)
                last_event_id = event.event_id
                n += 1
            # Persist the advanced cursor under the lock - only mutate if
            # the subscription still exists (unsubscribe may have raced).
            if last_event_id != cursor:
                with self._lock:
                    sub = self._subs.get(sub_id)
                    if sub is not None:
                        sub.cursor = last_event_id
            return n
        finally:
            with self._lock:
                self._polling.discard(sub_id)


__all__ = ["Subscription", "SubscriptionManager"]
