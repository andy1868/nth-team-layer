"""
Event Subscriptions — pub/sub pattern on top of EventBus.

Agents subscribe to event types with glob-style patterns and register
callbacks that fire when matching events are emitted.  The subscription
layer polls the EventBus periodically and delivers matching events.

Usage::

    subs = SubscriptionManager(event_bus=team.event_bus)

    # Subscribe to all group message events
    sub_id = subs.subscribe(
        "group.message.*",
        callback=lambda event: print(f"New message: {event.payload}"),
        subscriber_id="agent-alice",
    )

    # Poll for new events and deliver to callbacks
    delivered = subs.poll()

    # Unsubscribe
    subs.unsubscribe(sub_id)

    # List active subscriptions
    for sub in subs.list_subscriptions():
        print(sub.pattern, sub.callback)

Design
------

- Zero external dependencies — pure stdlib.
- Glob-style pattern matching (fnmatch) for event types.
- Callbacks are plain callables ``(BusEvent) -> Any``.
- Polling-based: call ``poll()`` to check for new events since last poll.
- Each subscription tracks its own cursor (last seen event_id).
- Subscriptions persisted as JSON for crash recovery.
- Thread-safe for concurrent subscribe/unsubscribe/poll.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .util.io import atomic_write_json, safe_load_json

if TYPE_CHECKING:
    from .event_bus import EventBus, BusEvent

logger = logging.getLogger("nth_dao.event_subscriptions")

DEFAULT_STORAGE_DIR = "team_subscriptions"


# ────────────────────────── Data types ──────────────────────────


@dataclass
class Subscription:
    """A single event subscription."""
    subscription_id: str
    pattern: str                  # glob pattern, e.g. "group.message.*"
    subscriber_id: str            # agent that created this subscription
    cursor: str = ""              # last delivered event_id
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def matches(self, event_type: str) -> bool:
        """Check if *event_type* matches this subscription's pattern."""
        return fnmatch.fnmatch(event_type, self.pattern)

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "pattern": self.pattern,
            "subscriber_id": self.subscriber_id,
            "cursor": self.cursor,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Subscription":
        return cls(
            subscription_id=data.get("subscription_id", ""),
            pattern=data.get("pattern", "*"),
            subscriber_id=data.get("subscriber_id", ""),
            cursor=data.get("cursor", ""),
            created_at=data.get("created_at", ""),
            metadata=data.get("metadata", {}),
        )


# ────────────────────────── Subscription Manager ──────────────────────────


class SubscriptionManager:
    """Manage event subscriptions on top of an EventBus.

    Parameters
    ----------
    event_bus : EventBus
        The team event bus to subscribe to.
    workspace : Path, optional
        Working directory for persistence.  Defaults to cwd.
    storage_dir : str
        Subdirectory under workspace.  Default ``"team_subscriptions"``.
    max_deliveries_per_poll : int
        Cap events delivered per ``poll()`` call.  Default 100.
    """

    DEFAULT_STORAGE_DIR = DEFAULT_STORAGE_DIR
    DEFAULT_MAX_DELIVERIES = 100

    def __init__(
        self,
        event_bus: "EventBus",
        *,
        workspace: Optional[Path] = None,
        storage_dir: str = DEFAULT_STORAGE_DIR,
        max_deliveries_per_poll: int = DEFAULT_MAX_DELIVERIES,
    ):
        self._event_bus = event_bus
        self._workspace = workspace or Path.cwd()
        self._max_deliveries = max(1, max_deliveries_per_poll)

        self._lock = threading.Lock()
        self._subscriptions: Dict[str, Subscription] = {}
        self._callbacks: Dict[str, Callable[["BusEvent"], Any]] = {}
        self._loaded = False

        self._state_dir = self._workspace / storage_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)

    # ── Registry ──────────────────────────────────────────

    def subscribe(
        self,
        pattern: str,
        callback: Callable[["BusEvent"], Any],
        *,
        subscriber_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        start_from: str = "",
    ) -> str:
        """Register a subscription and return its ID.

        Parameters
        ----------
        pattern : str
            Glob pattern for event types, e.g. ``"group.*"``, ``"*.posted"``.
            Use ``"*"`` for all events.
        callback : callable
            ``(BusEvent) -> Any``.  Called during ``poll()`` for matching events.
            **Must be re-registered after process restart** — callbacks are not
            persisted to disk (only subscription metadata survives).
        subscriber_id : str
            Agent creating this subscription (for ownership tracking).
        metadata : dict, optional
            Arbitrary key-value metadata.
        start_from : str
            Event ID to start replaying from.  If empty, starts from the
            earliest event in the bus.

        Returns
        -------
        subscription_id : str
            Unique identifier for unsubscribe / management.

        Note
        ----
        Cursor model: ``poll()`` advances *all* cursors to the latest
        delivered event, regardless of whether a subscription matched.
        This prevents a narrow subscription (e.g., ``"deploy.*"``) from
        stalling when only unrelated events are emitted.  If you need
        per-subscriber isolation, use ``poll_for()`` instead.  The two
        methods should not be mixed — ``poll()`` may advance cursors
        past events that ``poll_for()`` hasn't delivered yet.
        """
        with self._lock:
            self._ensure_loaded()
            sub_id = uuid.uuid4().hex[:12]
            sub = Subscription(
                subscription_id=sub_id,
                pattern=pattern,
                subscriber_id=subscriber_id,
                cursor=start_from,
                metadata=metadata or {},
            )
            self._subscriptions[sub_id] = sub
            self._callbacks[sub_id] = callback
            self._persist()
            logger.debug("subscribed %r → pattern=%r", sub_id, pattern)
            return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription.  Returns True if it existed."""
        with self._lock:
            self._ensure_loaded()
            existed = subscription_id in self._subscriptions
            self._subscriptions.pop(subscription_id, None)
            self._callbacks.pop(subscription_id, None)
            if existed:
                self._persist()
                logger.debug("unsubscribed %r", subscription_id)
            return existed

    def list_subscriptions(self, subscriber_id: str = "") -> List[Subscription]:
        """Return active subscriptions, optionally filtered by subscriber."""
        with self._lock:
            self._ensure_loaded()
            subs = list(self._subscriptions.values())
            if subscriber_id:
                subs = [s for s in subs if s.subscriber_id == subscriber_id]
            return sorted(subs, key=lambda s: s.created_at)

    def subscription(self, subscription_id: str) -> Optional[Subscription]:
        """Return a single subscription by ID, or None."""
        with self._lock:
            self._ensure_loaded()
            return self._subscriptions.get(subscription_id)

    # ── Polling ───────────────────────────────────────────

    def poll(self) -> List["BusEvent"]:
        """Poll EventBus for new events and deliver to matching callbacks.

        Returns the list of events that were successfully delivered.
        Each subscription's cursor advances past delivered events.

        Delivery order: events are replayed in chronological order.
        Each event is delivered to ALL matching subscriptions before
        moving to the next event.
        """
        with self._lock:
            self._ensure_loaded()
            if not self._subscriptions:
                return []

            # Determine the earliest cursor across all subscriptions
            earliest = self._earliest_cursor()
            if earliest:
                events = list(self._event_bus.replay(
                    from_id=earliest,
                    limit=self._max_deliveries,
                ))
            else:
                events = list(self._event_bus.replay(
                    limit=self._max_deliveries,
                ))

            if not events:
                return []

            # For each new event, deliver to matching subscriptions
            delivered: List["BusEvent"] = []
            last_event_id = ""
            for event in events:
                last_event_id = event.event_id
                matched_any = False
                for sub in self._subscriptions.values():
                    if sub.matches(event.event_type):
                        try:
                            cb = self._callbacks.get(sub.subscription_id)
                            if cb:
                                cb(event)
                            sub.cursor = event.event_id
                            matched_any = True
                        except Exception:
                            logger.exception(
                                "callback for sub %r raised on event %r",
                                sub.subscription_id, event.event_id,
                            )
                if matched_any:
                    delivered.append(event)

            # Advance cursors past the latest delivered event for all subs
            # that didn't match (so they don't stall the entire poll)
            if last_event_id:
                for sub in self._subscriptions.values():
                    if sub.cursor != last_event_id:
                        # Only advance if they haven't been advanced by delivery
                        if not sub.cursor or sub.cursor < last_event_id:
                            sub.cursor = last_event_id

            if delivered:
                self._persist()
            return delivered

    def poll_for(
        self,
        subscriber_id: str,
        limit: int = 50,
    ) -> List["BusEvent"]:
        """Poll and deliver events only for a specific subscriber's subscriptions.

        Other subscribers' cursors are NOT advanced.  Use this for
        per-agent polling loops.
        """
        with self._lock:
            self._ensure_loaded()
            subs = [
                s for s in self._subscriptions.values()
                if s.subscriber_id == subscriber_id
            ]
            if not subs:
                return []

            earliest = min(
                (s.cursor for s in subs if s.cursor),
                default="",
            )
            if earliest:
                events = list(self._event_bus.replay(
                    from_id=earliest, limit=limit,
                ))
            else:
                events = list(self._event_bus.replay(limit=limit))

            if not events:
                return []

            delivered: List["BusEvent"] = []
            for event in events:
                for sub in subs:
                    if sub.matches(event.event_type):
                        try:
                            cb = self._callbacks.get(sub.subscription_id)
                            if cb:
                                cb(event)
                            sub.cursor = event.event_id
                            if event not in delivered:
                                delivered.append(event)
                        except Exception:
                            logger.exception(
                                "callback for sub %r raised", sub.subscription_id,
                            )

            if delivered:
                self._persist()
            return delivered

    # ── Persistence ───────────────────────────────────────

    def _state_path(self) -> Path:
        return self._state_dir / "subscriptions.json"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        path = self._state_path()
        if path.exists():
            data = safe_load_json(path) or {}
            self._subscriptions = {}
            for raw in data.get("subscriptions", []):
                sub = Subscription.from_dict(raw)
                self._subscriptions[sub.subscription_id] = sub
        self._loaded = True

    def _persist(self) -> None:
        data = {
            "subscriptions": [s.to_dict() for s in self._subscriptions.values()],
        }
        atomic_write_json(self._state_path(), data)

    def _earliest_cursor(self) -> str:
        """Return the earliest non-empty cursor across all subscriptions."""
        cursors = [s.cursor for s in self._subscriptions.values() if s.cursor]
        return min(cursors) if cursors else ""

    def __repr__(self) -> str:
        with self._lock:
            self._ensure_loaded()
            return (
                f"SubscriptionManager(subscriptions={len(self._subscriptions)})"
            )
