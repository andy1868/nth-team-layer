"""Per-workspace persistent contact / agent-profile store.

The Day-1 ``/api/agents/add`` flow accepted ``target_did`` and derived
``agent_id``, but only persisted the ``agent_id`` into membership.
The DID itself, the pubkey, the label, and the discovery source were
all dropped on the floor. So adding a remote agent by DID looked like
it worked, but after a process restart the search row for that agent
had ``did=""`` and the operator could no longer reach them by DID.

``ContactBook`` is the missing layer. It stores one append-only JSONL
record per known agent with the four fields a search row needs to
display ("who is this?") plus a discovery-source tag for audit:

    {
        "agent_id":   "alice",
        "did":        "did:key:z6Mk...",
        "pubkey_hex": "9ca95ef5...",
        "label":      "Alice Wu",
        "source":     "manual_add" | "lan_discover" | "group_member"
                      | "agent_card" | "addressbook_import",
        "added_at":   "2026-06-08T12:34:56.789Z",
        "added_by":   "<the actor agent_id that performed the add>",
    }

Records are append-only - re-adding the same agent appends a new
record, and read paths deduplicate by ``agent_id`` keeping only the
latest. This mirrors the audit-by-default pattern the rest of NTH DAO
already uses (web-of-trust endorsements, event_bus events, channel
messages) so the file is replayable and an external auditor can
reconstruct "who first added whom, when, citing what source".

On-disk path:    <workspace>/team_contacts/contacts.jsonl

Read caching:   the file is the source of truth. We cache the parsed
                dict by mtime (mirroring _MtimeCache in web/__init__.py)
                so the hot-path /api/agents/search call doesn't re-read
                JSONL on every poll.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .util import (
    LOCK_TIMEOUT_PATIENT,
    safe_append_jsonl,
)

logger = logging.getLogger("nth_dao.contact_book")


# Source values are an enum-ish set of strings rather than a real Enum
# so JSON round-tripping is trivial and a future "this came from a
# protocol I don't recognise yet" entry doesn't break the reader.
SOURCE_MANUAL = "manual_add"
SOURCE_LAN = "lan_discover"
SOURCE_GROUP = "group_member"
SOURCE_AGENT_CARD = "agent_card"
SOURCE_IMPORT = "addressbook_import"


@dataclass
class ContactRecord:
    """One row in the contact book.

    All fields default to "" / current-time so a caller can add a
    sparse record (e.g. only agent_id known, DID not yet learned)
    without having to thread placeholder values.
    """

    agent_id: str
    did: str = ""
    pubkey_hex: str = ""
    label: str = ""
    source: str = SOURCE_MANUAL
    added_at: str = field(default_factory=lambda: _iso_now())
    added_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ContactRecord":
        # Tolerate unknown keys (forward compat with a future schema
        # extension) by filtering down to the dataclass field set.
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in fields})


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_CONTACTS_DIR = "team_contacts"
DEFAULT_CONTACTS_FILE = "contacts.jsonl"


class ContactBook:
    """Append-only contact store keyed by agent_id.

    Thread safety: read methods hold an internal lock around the
    mtime check + cache hit, so multiple FastAPI workers in the same
    process don't race on the cache dict. Write methods delegate to
    ``safe_append_jsonl`` which already serialises across processes
    via ``InterProcessLock``.

    Concurrency invariant: append-only writes mean a reader holding
    a stale parsed view sees fewer records than reality but never
    sees a TORN record. Records appearing during a read window will
    surface on the next mtime tick.
    """

    def __init__(self, workspace: Union[str, Path]):
        self.workspace = Path(workspace)
        self.base_dir = self.workspace / DEFAULT_CONTACTS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.base_dir / DEFAULT_CONTACTS_FILE

        # ── read cache ─────────────────────────────────────────────
        # mtime / size signature keyed by the on-disk file; when both
        # match the previous successful read, return the parsed map
        # directly without re-parsing JSONL.
        self._lock = threading.Lock()
        self._cached_view: Dict[str, ContactRecord] = {}
        self._cached_signature: Optional[tuple] = None

    @property
    def path(self) -> Path:
        return self._path

    # ── write ────────────────────────────────────────────────────────

    def add(
        self,
        agent_id: str,
        *,
        did: str = "",
        pubkey_hex: str = "",
        label: str = "",
        source: str = SOURCE_MANUAL,
        added_by: str = "",
    ) -> ContactRecord:
        """Append a contact record. Returns the persisted record.

        Caller contract: ``agent_id`` is required and must be a
        non-empty string. The other fields are optional - any subset
        of (did, pubkey_hex, label) may be empty if not yet known, and
        a later ``add()`` call for the same agent_id can fill the
        gaps. The reader returns whichever value is most-recently
        populated per field.
        """
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")

        record = ContactRecord(
            agent_id=agent_id.strip(),
            did=(did or "").strip(),
            pubkey_hex=(pubkey_hex or "").strip(),
            label=(label or "").strip(),
            source=(source or SOURCE_MANUAL).strip() or SOURCE_MANUAL,
            added_at=_iso_now(),
            added_by=(added_by or "").strip(),
        )
        # Persist with PATIENT timeout: contacts are a low-frequency
        # but high-value write - we never want to fail-open on lock
        # contention because the operator just clicked +Add and is
        # waiting to see the row.
        safe_append_jsonl(
            self._path, record.to_dict(),
            lock_timeout=LOCK_TIMEOUT_PATIENT,
        )
        # Invalidate cache so the next read picks up the new record.
        with self._lock:
            self._cached_signature = None
        return record

    # ── read ─────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> Optional[ContactRecord]:
        """Return the most-recent record for ``agent_id`` or None."""
        if not agent_id:
            return None
        return self._view().get(agent_id.strip())

    def find_by_did(self, did: str) -> Optional[ContactRecord]:
        """Reverse lookup by ``did``. Returns first match by agent_id
        ordering for stability."""
        if not did:
            return None
        target = did.strip()
        for record in self._view().values():
            if record.did and record.did == target:
                return record
        return None

    def find_by_pubkey(self, pubkey_hex: str) -> Optional[ContactRecord]:
        if not pubkey_hex:
            return None
        target = pubkey_hex.strip().lower()
        for record in self._view().values():
            if record.pubkey_hex and record.pubkey_hex.lower() == target:
                return record
        return None

    def list_all(self) -> List[ContactRecord]:
        """All known contacts, sorted by agent_id for deterministic
        output (search endpoints rely on stable ordering for paging)."""
        return sorted(self._view().values(), key=lambda r: r.agent_id)

    # ── internals ────────────────────────────────────────────────────

    def _view(self) -> Dict[str, ContactRecord]:
        """Return the deduplicated agent_id -> latest-record map.

        Cached by file (mtime, size). On signature mismatch we
        re-parse the JSONL; otherwise we return the cached dict.
        """
        signature = self._current_signature()
        with self._lock:
            if signature == self._cached_signature:
                return self._cached_view
            view = self._load_view()
            self._cached_view = view
            self._cached_signature = signature
            return view

    def _current_signature(self) -> tuple:
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return (0, 0)

    def _load_view(self) -> Dict[str, ContactRecord]:
        """Parse the JSONL and return latest-by-agent_id."""
        if not self._path.exists():
            return {}
        merged: Dict[str, ContactRecord] = {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line_no, raw in enumerate(f, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # A truncated tail line on an in-flight append
                        # is recoverable - skip and keep parsing. Log
                        # once at debug so a recurring corruption is
                        # still visible to operators.
                        logger.debug(
                            "skipping malformed line %d in %s",
                            line_no, self._path,
                        )
                        continue
                    try:
                        record = ContactRecord.from_dict(data)
                    except TypeError as exc:
                        logger.debug(
                            "skipping schema-incompatible line %d in %s: %s",
                            line_no, self._path, exc,
                        )
                        continue
                    if not record.agent_id:
                        continue
                    # Latest-wins merge: a later record overrides
                    # earlier ones for the same agent_id, but we also
                    # carry forward any field the new record left
                    # blank. This makes "sparse update" useful
                    # (e.g., a later record fills in a missing DID
                    # without forcing the caller to repeat label etc.)
                    prior = merged.get(record.agent_id)
                    if prior is None:
                        merged[record.agent_id] = record
                    else:
                        merged[record.agent_id] = _merge_records(prior, record)
        except OSError as exc:
            logger.warning(
                "could not read contact book %s: %s; returning empty view",
                self._path, exc,
            )
            return {}
        return merged


def _merge_records(
    prior: ContactRecord, newer: ContactRecord,
) -> ContactRecord:
    """Carry forward populated fields from the newer record, falling
    back to the prior record's value when the newer one left a slot
    blank.

    Intentionally NOT used as a general "diff" tool: this is purely
    about "later add() with partial info should not wipe out earlier
    info that's still useful". So an explicit empty did="" does NOT
    overwrite a prior did="did:key:zXYZ".

    ``added_at`` and ``added_by`` always reflect the NEWER row so the
    audit trail tracks the most recent touch.
    """
    return ContactRecord(
        agent_id=newer.agent_id,
        did=newer.did or prior.did,
        pubkey_hex=newer.pubkey_hex or prior.pubkey_hex,
        label=newer.label or prior.label,
        source=newer.source or prior.source,
        added_at=newer.added_at,
        added_by=newer.added_by or prior.added_by,
    )


__all__ = [
    "ContactBook",
    "ContactRecord",
    "SOURCE_MANUAL",
    "SOURCE_LAN",
    "SOURCE_GROUP",
    "SOURCE_AGENT_CARD",
    "SOURCE_IMPORT",
]
