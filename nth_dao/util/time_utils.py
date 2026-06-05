"""Time helpers — UTC-anchored ISO timestamps + monotonic elapsed measurement.

Why this exists
---------------
The original codebase wrote ``datetime.now().isoformat()`` everywhere,
producing **naive local time**. Two events emitted on two hosts in
different timezones sort wrong by lexicographic compare; the audit
chain disagrees with wall-clock truth. This module is the canonical
place to mint timestamps so the bug is fixed once rather than in
forty files.

Use::

    from nth_dao.util.time_utils import now_iso, monotonic_ms

    event.timestamp = now_iso()              # ISO-8601 UTC w/ +00:00 suffix
    start = monotonic_ms()
    work()
    elapsed = monotonic_ms() - start         # never negative, even with NTP jumps
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_iso() -> str:
    """UTC-anchored ISO-8601 timestamp.

    Always carries the timezone marker (``+00:00``), so lexicographic
    comparison agrees with wall-clock ordering across hosts and a parser
    can recover the absolute instant unambiguously.
    """
    return datetime.now(timezone.utc).isoformat()


def monotonic_ms() -> float:
    """Monotonic millisecond counter for elapsed-time measurement.

    Wall-clock (``datetime.now``) can jump backward under NTP corrections
    or sysadmin clock adjustments; monotonic cannot. Use this for any
    ``elapsed_ms`` field.
    """
    return time.monotonic() * 1000.0


__all__ = ["now_iso", "monotonic_ms"]
