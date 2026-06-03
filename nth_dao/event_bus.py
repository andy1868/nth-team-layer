"""EventBus — team-level signed, hash-chained append-only event stream.

Where ``AgentLedger`` records *one agent's* contribution events (per-pubkey
fingerprint), ``EventBus`` records *the team's* events across all
participating agents in a single chain. The two are orthogonal:

    AgentLedger      → "what did Alice do this month?"
    EventBus         → "what happened in this DAO, end-to-end?"

Storage layout::

    <workspace>/team_audit/
    ├── events.jsonl        # append-only signed + hash-chained events
    └── events.index.json   # event_id → byte offset (O(1) get)

Design decisions:

1.  **Hash chain** — every event carries ``prev_hash`` (the previous
    event's ``event_hash``) and ``event_hash = sha256(canonical_json(
    signable_dict()))``. Tampering with any historical event invalidates
    every event after it; the team can replay the stream and detect the
    cut point.

2.  **Optional Ed25519 signatures** — when the emitting identity has
    PyNaCl available, the signer signs ``signable_dict()`` and the
    signature lives in ``sig``. Unsigned events still chain, but
    ``verify()`` returns ``UNSIGNED`` so downstream code can decide
    trust on its own.

3.  **Cross-process safe** — every emit() takes an inter-process file
    lock on the events file. The append uses ``fsync`` and the index
    update goes through ``atomic_write_json`` so a crash during write
    leaves at most one trailing partial line (which the reader skips).

4.  **Streaming reads** — ``replay()`` is a generator. ``get(event_id)``
    is O(1) via the offset index, with a self-check that catches
    stale indices.

5.  **Deterministic stats** — ``agent_stats(fingerprint)`` and
    ``team_stats()`` fold the event stream into per-agent / cross-agent
    summaries. Same events ⇒ same dict on any implementation that
    re-reads ``events.jsonl``.

Original design contributed by @andy1868 in PR #7. Reworked here to:
chain events via prev_hash/event_hash; expose verify_chain(); guard
against partial writes via fsync + lock; add an explicit __all__ and
facade re-export.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from .identity import (
    AgentIdentity,
    _NACL_AVAILABLE,
    _VerifyKey,
    canonical_json,
)
from .util import (
    InterProcessLock,
    atomic_write_json,
    safe_load_json,
)

logger = logging.getLogger("nth_dao.event_bus")

DEFAULT_EVENTS_DIR = "team_audit"
DEFAULT_EVENTS_FILE = "events.jsonl"
DEFAULT_INDEX_FILE = "events.index.json"

ZERO_HASH = "0" * 64


def _is_hex(value: str, expected_len: int) -> bool:
    if not isinstance(value, str) or len(value) != expected_len:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def _fingerprint_of(pubkey_hex: str) -> str:
    """Same 16-char SHA-256 fingerprint AgentLedger uses; keeps both
    subsystems addressing the same agent under one identifier."""
    if not pubkey_hex:
        return ""
    return hashlib.sha256(pubkey_hex.encode("utf-8")).hexdigest()[:16]


class VerificationResult(str, Enum):
    """Explicit four-way verification outcome so callers can never
    silently treat an unsigned or unverifiable event as trusted."""

    VALID = "valid"               # signature OK
    INVALID = "invalid"           # signature present but rejected
    UNSIGNED = "unsigned"         # no sig — emitter was anonymous
    UNVERIFIABLE = "unverifiable"  # PyNaCl missing — cannot decide


class CorrectionType(str, Enum):
    """Standard semantic types for event corrections.

    These are agent-first error patterns — not human social UX
    (message recall, typo edits). Agents don't make typos; they make
    deterministic mistakes:

    - ``DEPRECATED`` — the event was valid at the time but is no longer
      actionable (e.g. a deployment URL that has since rotated).
    - ``CORRECTED`` — the event carried wrong data; the correction
      carries ``corrected_payload`` with the right values.
    - ``RETRACTED`` — the event should not have been emitted at all
      (e.g. it was produced by a compromised credential). The audit
      trail preserves the original; consumers MUST treat it as void.
    """

    DEPRECATED = "DEPRECATED"
    CORRECTED = "CORRECTED"
    RETRACTED = "RETRACTED"


@dataclass
class BusEvent:
    """One signed, hash-chained event on the team stream.

    Fields:
        event_id:      16-hex unique id (uuid4 short).
        event_type:    "group.message.posted" / "mission.step.completed" /
                       etc. Open vocabulary; subsystems define their own.
        actor_id:      Human-readable id of the emitter (matches the
                       AgentID layer; not a security boundary on its own).
        actor_pubkey:  64-hex Ed25519 public key when signed; empty when
                       not. ``actor_id`` derived from it via fingerprint.
        payload:       Event-type-specific dict; canonical_json-stable.
        timestamp:     ISO-8601, naive local time (matches AgentLedger).
        seq:           1-based monotonic position on this stream.
        prev_hash:     event_hash of the previous event; ZERO_HASH for #1.
        event_hash:    sha256(canonical_json(signable_dict())).
        sig:           128-hex Ed25519 signature over signable_dict(),
                       or empty for unsigned events.
    """

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    event_type: str = ""
    actor_id: str = ""
    actor_pubkey: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    seq: int = 0
    prev_hash: str = ZERO_HASH
    event_hash: str = ""
    sig: str = ""

    def signable_dict(self) -> Dict[str, Any]:
        """The bytes that get signed AND hashed. ``sig`` and ``event_hash``
        are excluded — they describe THIS step rather than belong to it."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "actor_pubkey": self.actor_pubkey,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "seq": self.seq,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.signable_dict())).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BusEvent":
        pubkey = data.get("actor_pubkey", "")
        sig = data.get("sig", "")
        if pubkey and not _is_hex(pubkey, 64):
            raise ValueError(
                f"actor_pubkey must be 64 hex chars, got "
                f"{len(pubkey)}-char value '{pubkey[:20]}…'"
            )
        if sig and not _is_hex(sig, 128):
            raise ValueError(
                f"sig must be 128 hex chars, got "
                f"{len(sig)}-char value '{sig[:20]}…'"
            )
        return cls(
            event_id=data.get("event_id", uuid.uuid4().hex[:16]),
            event_type=data.get("event_type", ""),
            actor_id=data.get("actor_id", ""),
            actor_pubkey=pubkey,
            payload=dict(data.get("payload", {})),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            seq=int(data.get("seq", 0)),
            prev_hash=data.get("prev_hash", ZERO_HASH),
            event_hash=data.get("event_hash", ""),
            sig=sig,
        )


class EventBus:
    """Append-only, hash-chained, optionally signed team event stream.

    Usage::

        bus = EventBus(workspace, identity=alice)
        bus.emit("group.message.posted", {"channel_id": "general",
                                          "body": "hello"})
        for ev in bus.replay(event_types=["group.message.posted"]):
            print(ev.event_id, ev.payload)

        ok, reason = bus.verify_chain()
        assert ok, reason
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        identity: Optional[AgentIdentity] = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.identity = identity

    # ─── filesystem properties ───

    @property
    def events_dir(self) -> Path:
        return self.workspace / DEFAULT_EVENTS_DIR

    @property
    def events_path(self) -> Path:
        return self.events_dir / DEFAULT_EVENTS_FILE

    @property
    def index_path(self) -> Path:
        return self.events_dir / DEFAULT_INDEX_FILE

    @property
    def can_sign(self) -> bool:
        return bool(self.identity and self.identity.can_sign)

    # ─── emit ───

    def emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        identity: Optional[AgentIdentity] = None,
    ) -> BusEvent:
        """Append one event to the stream. Chain + (optional) signature
        are computed under the file lock so concurrent emitters can't
        race the seq / prev_hash linkage."""
        signer = identity or self.identity
        actor_id = ""
        actor_pubkey = ""
        if signer is not None:
            actor_id = str(signer.agent_id)
            actor_pubkey = signer.pubkey_hex if signer.can_sign else ""

        self.events_dir.mkdir(parents=True, exist_ok=True)

        with InterProcessLock(self.events_path.with_suffix(".jsonl.lock")):
            previous_seq, previous_hash = self._tail_unlocked()
            event = BusEvent(
                event_type=event_type,
                actor_id=actor_id,
                actor_pubkey=actor_pubkey,
                payload=dict(payload or {}),
                seq=previous_seq + 1,
                prev_hash=previous_hash,
            )
            event.event_hash = event.compute_hash()
            if _NACL_AVAILABLE and signer is not None and signer.can_sign:
                try:
                    event.sig = signer.sign_json(event.signable_dict())
                except Exception as exc:
                    logger.warning("sign failed for event %s: %s", event.event_id, exc)
                    event.sig = ""

            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            line_bytes = line.encode("utf-8") + b"\n"
            offset = self.events_path.stat().st_size if self.events_path.exists() else 0
            with self.events_path.open("ab") as fh:
                fh.write(line_bytes)
                fh.flush()
                os.fsync(fh.fileno())

            index = self._load_index()
            index[event.event_id] = offset
            atomic_write_json(self.index_path, index)

        return event

    def correct(
        self,
        original_event_id: str,
        correction_type: CorrectionType,
        *,
        reason: str = "",
        corrected_payload: Optional[Dict[str, Any]] = None,
        identity: Optional[AgentIdentity] = None,
    ) -> BusEvent:
        """Emit an ``event.correction`` that references a prior event.

        The original event is NEVER deleted or mutated — it stays in the
        stream as an auditable record. Consumers reading the stream should
        check ``get_corrections_for()`` to discover whether an event they
        are about to act on has been superseded.

        ``correction_type`` is one of ``CorrectionType``:

        - ``DEPRECATED`` — still true but no longer actionable.
        - ``CORRECTED`` — the original data was wrong; see
          ``corrected_payload`` for the right version.
        - ``RETRACTED`` — the original event MUST be treated as void
          (compromised credential, faulty agent run, etc.).

        ``reason`` is human-readable but optional. ``corrected_payload``
        carries the fixed data and is only meaningful with CORRECTED.

        Raises ``ValueError`` if ``original_event_id`` is empty.
        """
        if not original_event_id or not original_event_id.strip():
            raise ValueError("original_event_id must be non-empty")

        payload: Dict[str, Any] = {
            "original_event_id": original_event_id,
            "correction_type": correction_type.value,
            "reason": reason,
        }
        if corrected_payload is not None:
            payload["corrected_payload"] = corrected_payload

        return self.emit("event.correction", payload, identity=identity)

    def get_corrections_for(self, original_event_id: str) -> Iterator[BusEvent]:
        """Yield every ``event.correction`` that references the given
        ``original_event_id``, in stream order.

        The stream MAY contain multiple corrections for one event
        (e.g. first DEPRECATED, later RETRACTED). Consumers should
        normally act on the *last* correction.
        """
        if not original_event_id:
            return
        for event in self.replay(event_types=["event.correction"]):
            p = event.payload
            if isinstance(p, dict) and p.get("original_event_id") == original_event_id:
                yield event

    # ─── internal ───

    def _tail_unlocked(self) -> Tuple[int, str]:
        """Walk the file once to the last well-formed line. Returns
        ``(seq, event_hash)`` of the last event, or ``(0, ZERO_HASH)``
        for an empty / missing stream. Called while holding the lock
        so no concurrent emit can interleave."""
        if not self.events_path.exists():
            return 0, ZERO_HASH
        last_seq = 0
        last_hash = ZERO_HASH
        try:
            with self.events_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Trailing partial write from a crash — ignore;
                        # next emit naturally continues the chain.
                        continue
                    last_seq = int(data.get("seq", last_seq))
                    last_hash = data.get("event_hash", last_hash) or last_hash
        except OSError as exc:
            logger.warning("tail scan failed: %s", exc)
        return last_seq, last_hash

    # ─── read ───

    def replay(
        self,
        from_id: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        actor_id: Optional[str] = None,
        limit: Optional[int] = None,
        reverse: bool = False,
    ) -> Iterator[BusEvent]:
        """Yield events in stream order. With ``from_id`` set, start
        *after* that event. ``reverse=True`` buffers the filtered set
        and yields in reverse order (memory cost = filtered count)."""
        if not self.events_path.exists():
            return
        if reverse:
            events = list(self._iter_events(from_id, event_types, actor_id))
            events.reverse()
            yield from (events[:limit] if limit else events)
            return
        count = 0
        for event in self._iter_events(from_id, event_types, actor_id):
            yield event
            count += 1
            if limit and count >= limit:
                break

    def _iter_events(
        self,
        from_id: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        actor_id: Optional[str] = None,
    ) -> Iterator[BusEvent]:
        try:
            with self.events_path.open("r", encoding="utf-8") as fh:
                started = from_id is None
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("corrupt event line in %s, skipping", self.events_path)
                        continue
                    if not started:
                        if data.get("event_id") == from_id:
                            started = True
                        continue
                    try:
                        event = BusEvent.from_dict(data)
                    except ValueError as exc:
                        logger.warning("malformed event skipped: %s", exc)
                        continue
                    if event_types and event.event_type not in event_types:
                        continue
                    if actor_id and event.actor_id != actor_id:
                        continue
                    yield event
        except OSError as exc:
            logger.warning("error reading events from %s: %s", self.events_path, exc)

    def get(self, event_id: str) -> Optional[BusEvent]:
        """O(1) point lookup via the offset index. Self-validates to
        catch stale indices (returns None rather than the wrong event)."""
        index = self._load_index()
        offset = index.get(event_id)
        if offset is None:
            return None
        try:
            with self.events_path.open("rb") as fh:
                fh.seek(offset)
                line = fh.readline().decode("utf-8").strip()
                if not line:
                    return None
                event = BusEvent.from_dict(json.loads(line))
                if event.event_id != event_id:
                    logger.warning(
                        "index offset %d for %s returned event %s — index stale",
                        offset, event_id, event.event_id,
                    )
                    return None
                return event
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("failed to read event %s at offset %d: %s",
                           event_id, offset, exc)
            return None

    # ─── verify ───

    def verify(self, event: BusEvent) -> VerificationResult:
        """Check one event's Ed25519 signature in isolation. Does NOT
        check chain integrity — use ``verify_chain()`` for that."""
        if not event.sig:
            return VerificationResult.UNSIGNED
        if not _is_hex(event.actor_pubkey, 64):
            return VerificationResult.INVALID
        if not _NACL_AVAILABLE:
            return VerificationResult.UNVERIFIABLE
        try:
            assert _VerifyKey is not None
            _VerifyKey(bytes.fromhex(event.actor_pubkey)).verify(
                canonical_json(event.signable_dict()),
                bytes.fromhex(event.sig),
            )
            return VerificationResult.VALID
        except Exception:
            return VerificationResult.INVALID

    def verify_chain(self) -> Tuple[bool, str]:
        """Walk the whole stream and confirm every event satisfies:

        - ``seq == previous_seq + 1`` (no gaps, no duplicates)
        - ``prev_hash == previous_event.event_hash`` (chain integrity)
        - ``event_hash == compute_hash()`` (no payload tampering)
        - if ``sig`` present: signature verifies under ``actor_pubkey``

        Returns ``(ok, reason)``. First failure short-circuits with
        the offending event_id in the reason — perfect for forensic
        diffing against a suspected-good copy."""
        if not self.events_path.exists():
            return True, "ok (empty)"
        expected_seq = 1
        prev_hash = ZERO_HASH
        unverifiable_seen = False
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    return False, f"corrupt JSON on line {line_no}: {exc}"
                try:
                    event = BusEvent.from_dict(data)
                except ValueError as exc:
                    return False, f"malformed event on line {line_no}: {exc}"
                if event.seq != expected_seq:
                    return False, (
                        f"seq mismatch at {event.event_id}: "
                        f"expected {expected_seq}, got {event.seq}"
                    )
                if event.prev_hash != prev_hash:
                    return False, f"prev_hash mismatch at {event.event_id}"
                if event.event_hash != event.compute_hash():
                    return False, f"event_hash mismatch at {event.event_id}"
                if event.sig:
                    result = self.verify(event)
                    if result == VerificationResult.INVALID:
                        return False, f"signature invalid at {event.event_id}"
                    if result == VerificationResult.UNVERIFIABLE:
                        unverifiable_seen = True
                prev_hash = event.event_hash
                expected_seq += 1
        if unverifiable_seen:
            return True, "ok (some events unverifiable — install PyNaCl to recheck)"
        return True, "ok"

    def verify_all(
        self, event_types: Optional[List[str]] = None
    ) -> Tuple[int, int, int, int]:
        """Per-event signature audit; returns ``(total, valid, invalid,
        unverifiable)``. Independent of chain integrity — pair with
        ``verify_chain()`` for the full picture."""
        total = valid = invalid = unverifiable = 0
        for event in self.replay(event_types=event_types):
            total += 1
            result = self.verify(event)
            if result == VerificationResult.VALID:
                valid += 1
            elif result == VerificationResult.INVALID:
                invalid += 1
            elif result == VerificationResult.UNVERIFIABLE:
                unverifiable += 1
        return total, valid, invalid, unverifiable

    # ─── stats ───

    def agent_stats(
        self,
        fingerprint: str,
        since: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fold the stream into one agent's contribution summary.
        Mirrors AgentLedger.stats() field names so consumers can fall
        back interchangeably."""
        stats: Dict[str, Any] = {
            "fingerprint": fingerprint,
            "missions_owned": 0,
            "steps_completed": 0,
            "steps_failed": 0,
            "reviews_given": 0,
            "endorsements_given": 0,
            "messages_sent": 0,
            "tasks_created": 0,
            "last_active_at": "",
            "total_events": 0,
            "since": since or "",
        }
        for event in self._stream_raw():
            actor_pubkey = event.get("actor_pubkey", "")
            if not actor_pubkey or _fingerprint_of(actor_pubkey) != fingerprint:
                continue
            ts = event.get("timestamp", "")
            if since and ts < since:
                continue
            stats["total_events"] += 1
            if ts > stats["last_active_at"]:
                stats["last_active_at"] = ts
            etype = event.get("event_type", "")
            if etype == "agent_ledger.step.completed":
                stats["steps_completed"] += 1
            elif etype == "agent_ledger.step.failed":
                stats["steps_failed"] += 1
            elif etype == "agent_ledger.review.given":
                stats["reviews_given"] += 1
            elif etype == "wot.endorsed":
                stats["endorsements_given"] += 1
            elif etype == "group.message.posted":
                stats["messages_sent"] += 1
            elif etype in ("group.task.created", "task.created"):
                stats["tasks_created"] += 1
            elif etype == "agent_ledger.mission.owned":
                stats["missions_owned"] += 1
        return stats

    def team_stats(self) -> Dict[str, Any]:
        """Cross-agent rollup: agent_count + total_events + per-agent
        event counts + last_active_at. Useful as a cheap dashboard
        endpoint without materialising the whole stream client-side."""
        agents: Dict[str, Dict[str, Any]] = {}
        total = 0
        for event in self._stream_raw():
            total += 1
            actor_pubkey = event.get("actor_pubkey", "")
            fp = _fingerprint_of(actor_pubkey) if actor_pubkey else ""
            if not fp:
                continue
            slot = agents.setdefault(fp, {
                "fingerprint": fp,
                "actor_id": event.get("actor_id", ""),
                "events": 0,
                "last_active_at": "",
            })
            slot["events"] += 1
            ts = event.get("timestamp", "")
            if ts > slot["last_active_at"]:
                slot["last_active_at"] = ts
        return {"agent_count": len(agents), "total_events": total, "agents": agents}

    def count(self, event_type: Optional[str] = None) -> int:
        n = 0
        for event in self._stream_raw():
            if event_type is None or event.get("event_type") == event_type:
                n += 1
        return n

    # ─── helpers ───

    def _stream_raw(self) -> Iterator[Dict[str, Any]]:
        """Yield raw dicts (faster than instantiating BusEvent) for
        analytics paths that don't need typed access."""
        if not self.events_path.exists():
            return
        try:
            with self.events_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning("stream read failed: %s", exc)

    def _load_index(self) -> Dict[str, int]:
        data = safe_load_json(self.index_path, fallback=None)
        if not isinstance(data, dict):
            return {}
        index: Dict[str, int] = {}
        for k, v in data.items():
            if isinstance(v, int):
                index[str(k)] = v
            elif isinstance(v, float):
                logger.warning("index %s has float value %.1f — treated as corrupt", k, v)
        return index


__all__ = [
    "BusEvent",
    "CorrectionType",
    "EventBus",
    "VerificationResult",
    "ZERO_HASH",
    "DEFAULT_EVENTS_DIR",
    "DEFAULT_EVENTS_FILE",
    "DEFAULT_INDEX_FILE",
]
