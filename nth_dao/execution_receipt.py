"""Signed execution receipts — L1-1 work-proof primitive (2026-06-08).

Strategic alignment: this module implements the wire format and
signing rules of motebit's ``execution-ledger@1.0`` spec
(motebit/motebit ``spec/execution-ledger-v1.md``) so a receipt
produced by an NTH DAO node verifies against any motebit consumer,
and vice versa. The goal: "工作量证明" — a third party can verify
that this agent really did the work it claims, without trusting NTH's
filesystem or even talking to the NTH node again.

Why a NEW module rather than extending ``nth_dao/agent_ledger.py``:
  * agent_ledger is long-lived per-agent reputation accumulation
    (jsonl append, fingerprint-scoped, reducer-based stats)
  * an execution receipt is a per-GOAL atomic, signed artifact —
    one document per finished execution, not a stream
  * the canonicalization rules + signing input differ (motebit
    requires a very specific newline-joined per-entry canonical
    form that agent_ledger doesn't speak)

Conflating them would either pollute agent_ledger's reducer or
sacrifice motebit interop. Keep separate; they CAN cite each other
later (e.g. agent_ledger could record "completed receipt X" pointers).

═══════════════════════════════════════════════════════════════════
Motebit execution-ledger@1.0 wire format (quoted from spec §5–§6)
═══════════════════════════════════════════════════════════════════

Per-entry canonical JSON:
  * keys sorted lexicographically
  * no whitespace (no spaces after ``:`` or ``,``)
  * all three fields present (payload, timestamp, type)
  * nested objects also sorted

Example entry (verbatim from spec):
  {"payload":{"goal_id":"goal-01","prompt":"Search for flights"},"timestamp":1710288000000,"type":"goal_started"}

Content hash:
  1. Canonicalize each entry individually
  2. Join with ``\\n`` (U+000A)
  3. SHA-256 over the resulting UTF-8 bytes
  4. Encode as lowercase hex (64 chars)

Signature:
  * ``signature = Ed25519_Sign(content_hash_bytes, private_key)``
  * **Signed payload is the 32-byte raw hash digest**, NOT its hex
    representation. Implementers who sign the hex string by mistake
    produce signatures that no motebit verifier will accept.
  * Encoded as base64url (RFC 4648 §5, no padding) — alphabet uses
    ``-`` and ``_`` instead of ``+`` and ``/``

Timestamps:
  * Integer milliseconds since Unix epoch (NOT float seconds)
  * Verified against the spec example: ``1710288000000``

═══════════════════════════════════════════════════════════════════
NTH envelope on top of motebit's signed core
═══════════════════════════════════════════════════════════════════

A motebit receipt = ``content_hash`` + ``signature`` + ``timeline``.
We wrap that core in an outer NTH envelope so an NTH-only consumer
gets discovery metadata (kind, receipt_id, signer DID) without
needing a separate index:

    {
      "kind": "nth-execution-receipt-v1",
      "compatible_with": "motebit/execution-ledger@1.0",
      "receipt_id": "<uuid>",
      "goal_id": "<caller-supplied>",
      "signer_did": "did:key:z…",
      "signer_pubkey_hex": "<64 hex>",
      "issued_at": "<ISO, display only>",

      # ── motebit core (these are what get verified) ────────────
      "timeline": [<entry>, …],
      "content_hash": "<64 hex>",
      "sig": "<base64url>"
    }

The envelope fields (kind, receipt_id, goal_id, signer_did,
signer_pubkey_hex, issued_at) are NOT covered by ``sig``. They are
discovery metadata only. The signature covers ``content_hash``, and
``content_hash`` covers ``timeline`` — together they bind the agent
to its claimed execution history. An attacker who edits ``timeline``
invalidates the hash; one who edits ``content_hash`` invalidates the
sig; one who edits envelope fields is just lying about discovery
metadata, which is the consumer's responsibility to cross-check
(e.g. via the DID's published identity card).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from nth_dao.b64u import b64u_decode, b64u_encode
from nth_dao.canonical_json import canonical_json
from nth_dao.did_key import decode_ed25519_did_key_hex, is_did_key
from nth_dao.identity import _NACL_AVAILABLE

try:
    from nacl.signing import VerifyKey as _VerifyKey
except ImportError:
    _VerifyKey = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from nth_dao.identity import AgentIdentity

logger = logging.getLogger("nth_dao.execution_receipt")


# ─── motebit-base event type vocabulary ──────────────────────────────
# Quoted verbatim from execution-ledger-v1.md §4.

TYPE_GOAL_STARTED = "goal_started"
TYPE_PLAN_CREATED = "plan_created"
TYPE_STEP_STARTED = "step_started"
TYPE_TOOL_INVOKED = "tool_invoked"
TYPE_TOOL_RESULT = "tool_result"
TYPE_STEP_COMPLETED = "step_completed"
TYPE_STEP_FAILED = "step_failed"
TYPE_STEP_DELEGATED = "step_delegated"
TYPE_PLAN_COMPLETED = "plan_completed"
TYPE_PLAN_FAILED = "plan_failed"
TYPE_GOAL_COMPLETED = "goal_completed"

MOTEBIT_BASE_TYPES = frozenset({
    TYPE_GOAL_STARTED, TYPE_PLAN_CREATED, TYPE_STEP_STARTED,
    TYPE_TOOL_INVOKED, TYPE_TOOL_RESULT, TYPE_STEP_COMPLETED,
    TYPE_STEP_FAILED, TYPE_STEP_DELEGATED, TYPE_PLAN_COMPLETED,
    TYPE_PLAN_FAILED, TYPE_GOAL_COMPLETED,
})

# NTH-specific extensions, namespaced under ``nth.`` per motebit's
# convention for non-base types ("Implementations MAY define
# additional values using a namespaced format").
TYPE_NTH_POST_MESSAGE = "nth.post_message"
TYPE_NTH_ADD_MEMBER = "nth.add_member"
TYPE_NTH_VOTE = "nth.vote"
TYPE_NTH_DAO_CREATED = "nth.dao_created"
TYPE_NTH_MANDATE_SIGNED = "nth.mandate_signed"

NTH_EXTENSION_TYPES = frozenset({
    TYPE_NTH_POST_MESSAGE, TYPE_NTH_ADD_MEMBER, TYPE_NTH_VOTE,
    TYPE_NTH_DAO_CREATED, TYPE_NTH_MANDATE_SIGNED,
})

# Wire versioning per motebit convention (``family/version@major.minor``)
NTH_RECEIPT_SPEC = "nth-dao/execution-receipt@1.0"
MOTEBIT_COMPATIBLE = "motebit/execution-ledger@1.0"
NTH_RECEIPT_KIND = "nth-execution-receipt-v1"


# ─── timeline entry ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TimelineEntry:
    """One timeline entry per motebit execution-ledger@1.0 §3.1.

    Attributes:
        timestamp: Unix epoch milliseconds (integer). Motebit pins
            integer ms (e.g. ``1710288000000``), NOT float seconds —
            float precision would make signatures non-portable across
            float-naive verifiers.
        type: Event type identifier. Either a motebit base type (see
            ``MOTEBIT_BASE_TYPES``) or an NTH-namespaced extension.
        payload: Arbitrary JSON-serializable dict. Keys may be in any
            order — canonical_json sorts them at sign time. Avoid
            float values inside (canonical_json rejects floats per the
            project-wide rule, same one that motebit also enforces).
    """

    timestamp: int
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defence in depth — both contract violations would silently
        # produce signatures that no motebit verifier accepts.
        if not isinstance(self.timestamp, int):
            raise TypeError(
                f"timestamp must be int (epoch ms); got "
                f"{type(self.timestamp).__name__}"
            )
        if self.timestamp < 0:
            raise ValueError(f"timestamp must be non-negative; got {self.timestamp}")
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("type must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a dict")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": dict(self.payload),  # defensive copy
        }


def now_ms() -> int:
    """Current Unix time in integer milliseconds.

    Helper for callers that don't want to import ``time`` themselves
    and risk drifting between ms/seconds/ns by mistake.

    MA-1 (review fix 2026-06-08): use ``time.time_ns() // 1_000_000``
    rather than ``int(time.time() * 1000)``. On Windows ``time.time()``
    has ~15 ms resolution; back-to-back ``now_ms()`` calls in the same
    receipt emission produced identical timestamps, which the motebit
    spec allows ("entries with equal timestamps MUST preserve insertion
    order") but a precise source is preferable — ``time_ns()`` is
    nanosecond-resolution on every platform Python supports.
    """
    return time.time_ns() // 1_000_000


# ─── canonical content hash ──────────────────────────────────────────


def _hash_and_dicts(
    timeline: List[TimelineEntry],
) -> "tuple[str, bytes, List[Dict[str, Any]]]":
    """Internal one-pass helper: hash the timeline AND return both
    the digest representations + the per-entry dicts.

    MA-3 (review fix 2026-06-08): the previous flow asked
    ``compute_content_hash`` to do ``entry.to_dict()`` per entry
    purely for hashing, and then ``sign_receipt`` re-built the same
    per-entry dicts for the output payload — two passes for what is
    a single producer side. Returning the dicts alongside the hash
    lets ``sign_receipt`` reuse the work.

    Returns:
        (hex_str, raw_32_byte_digest, entry_dicts)

        * ``hex_str``: 64-char lowercase hex (the public wire form)
        * ``raw_32_byte_digest``: the SHA-256 output bytes — exactly
          what motebit-spec §6 signs; avoids a ``bytes.fromhex`` round
          trip
        * ``entry_dicts``: the per-entry Python dicts the caller can
          slot directly into the receipt envelope's ``timeline`` field

    Raises:
        ValueError if the timeline is empty.
    """
    if not timeline:
        raise ValueError(
            "cannot hash empty timeline; receipts require at least "
            "one entry (motebit recommends starting with goal_started)"
        )
    entry_dicts: List[Dict[str, Any]] = [e.to_dict() for e in timeline]
    per_entry: List[bytes] = [canonical_json(d) for d in entry_dicts]
    joined = b"\n".join(per_entry)
    h = hashlib.sha256(joined)
    return h.hexdigest(), h.digest(), entry_dicts


def compute_content_hash(timeline: List[TimelineEntry]) -> str:
    """Compute the motebit-spec content_hash for a timeline.

    Steps (per execution-ledger@1.0 §5):
      1. Canonicalize each entry: sort keys, no whitespace, UTF-8.
      2. Join the per-entry canonical bytes with ``\\n`` (U+000A).
      3. SHA-256 the joined bytes.
      4. Lowercase hex.

    The result is byte-stable across implementations: a Rust port
    that follows the same recipe will compute the same hash, which is
    the whole point of having a wire spec.

    Public API contract: returns the 64-char hex string. Internal
    callers that ALSO need the raw 32-byte digest (e.g. ``sign_receipt``,
    which must sign the digest per spec §6) should call the
    private ``_hash_and_dicts`` helper instead — that's the only
    spot in the codebase where a single canonicalization pass is
    actually visible.

    Raises:
        ValueError if the timeline is empty — an empty receipt is
        meaningless and the spec implies at least one entry (goal must
        have a ``goal_started`` event minimum).
    """
    hex_str, _, _ = _hash_and_dicts(timeline)
    return hex_str


# ─── base64url helpers: CR-1 fix (2026-06-08) ────────────────────────
# Shared codec lives in ``nth_dao.b64u`` to prevent the per-module
# drift that motivated this refactor. Keep the local names for
# call-site readability without re-implementing the bodies.

_b64u = b64u_encode
_b64u_decode = b64u_decode


# ─── build + sign ────────────────────────────────────────────────────


def sign_receipt(
    timeline: List[TimelineEntry],
    identity: "AgentIdentity",
    *,
    goal_id: str = "",
    receipt_id: str = "",
) -> Dict[str, Any]:
    """Build a signed execution receipt over ``timeline``.

    The signature input is the **32-byte raw hash digest** (NOT the
    hex string) per motebit execution-ledger@1.0 §6 — implementations
    that sign the hex string produce sigs no motebit verifier accepts.

    Args:
        timeline: ordered list of ``TimelineEntry`` rows. Caller is
            responsible for ordering — typically by ``timestamp``.
            The spec says "Entries with equal timestamps MUST preserve
            insertion order" so we trust the caller's list order
            after the timestamp sort.
        identity: the signing ``AgentIdentity``. Must have a private
            key (``identity.can_sign``); raises if not.
        goal_id: caller-supplied opaque ID linking this receipt to
            the goal/mission/task it terminates. Empty string is OK
            for ad-hoc receipts.
        receipt_id: caller-supplied UUID. If empty, a fresh uuid4 is
            minted. The receipt_id is for discovery only — it is NOT
            covered by ``sig``.

    Returns:
        A dict matching the NTH-execution-receipt-v1 envelope (see
        module docstring for shape).

    Raises:
        RuntimeError if the identity cannot sign.
    """
    # MA-3 (review fix 2026-06-08): single canonicalization pass.
    # ``_hash_and_dicts`` produces the hex, the raw 32-byte digest,
    # and the per-entry dicts in one walk over the timeline. The
    # previous implementation called ``compute_content_hash`` (which
    # internally built per-entry dicts) and THEN ran
    # ``[e.to_dict() for e in timeline]`` again purely to populate
    # the envelope's ``timeline`` field — double work for no benefit.
    # We also drop the ``bytes.fromhex(content_hash)`` round-trip
    # since ``digest`` is already the bytes we want to sign.
    content_hash, digest_bytes, entry_dicts = _hash_and_dicts(timeline)
    sig_bytes = identity.sign(digest_bytes)
    sig_b64 = _b64u(sig_bytes)

    return {
        "kind": NTH_RECEIPT_KIND,
        "spec": NTH_RECEIPT_SPEC,
        "compatible_with": MOTEBIT_COMPATIBLE,
        "receipt_id": receipt_id or uuid.uuid4().hex,
        "goal_id": goal_id,
        "signer_did": identity.as_did(),
        "signer_pubkey_hex": identity.pubkey_hex,
        "issued_at": datetime.now().isoformat(),
        # ── motebit core (what gets verified) ─────────────────────
        "timeline": entry_dicts,
        "content_hash": content_hash,
        "sig": sig_b64,
    }


# ─── verify ──────────────────────────────────────────────────────────


def verify_receipt(
    receipt: Dict[str, Any],
    *,
    expected_pubkey_hex: str = "",
) -> bool:
    """Verify a receipt's content_hash + signature.

    A receipt is valid iff ALL of:
      1. ``timeline`` is well-formed (list of entries with the three
         required fields, types correct).
      2. Recomputed ``content_hash`` matches the stored one (binds
         the timeline to its claimed hash).
      3. Signature verifies under the pubkey derived from
         ``signer_did`` (binds the agent to the timeline).
      4. If ``expected_pubkey_hex`` is supplied, the pubkey derived
         from ``signer_did`` matches it (belt-and-braces for callers
         that already know the agent's pubkey via another channel).

    Returns False on any failure rather than raising — verification
    is a yes/no operation and callers should not need to handle a
    bouquet of exceptions to ask "is this receipt good?". If you
    need to KNOW why a verification failed, run with logger at
    DEBUG.
    """
    if not _NACL_AVAILABLE or _VerifyKey is None:
        return False

    timeline_data = receipt.get("timeline")
    if not isinstance(timeline_data, list) or not timeline_data:
        logger.debug("receipt has no timeline")
        return False
    try:
        timeline = [
            TimelineEntry(
                timestamp=int(e["timestamp"]),
                type=str(e["type"]),
                payload=dict(e.get("payload", {})),
            )
            for e in timeline_data
        ]
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("receipt timeline malformed: %s", exc)
        return False

    try:
        recomputed = compute_content_hash(timeline)
    except Exception as exc:  # noqa: BLE001
        logger.debug("compute_content_hash failed: %s", exc)
        return False
    claimed_hash = receipt.get("content_hash", "")
    if recomputed != claimed_hash:
        logger.debug(
            "content_hash mismatch: recomputed %s vs claimed %s",
            recomputed, claimed_hash,
        )
        return False

    sig_b64 = receipt.get("sig", "")
    if not sig_b64:
        return False
    try:
        sig_bytes = _b64u_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sig base64url decode failed: %s", exc)
        return False

    did = receipt.get("signer_did", "")
    if not did or not is_did_key(did):
        logger.debug("signer_did missing or not did:key")
        return False
    pubkey_hex = decode_ed25519_did_key_hex(did) or ""
    if not pubkey_hex:
        return False
    if (
        expected_pubkey_hex
        and pubkey_hex.lower() != expected_pubkey_hex.lower()
    ):
        logger.debug(
            "signer_did pubkey %s != expected %s",
            pubkey_hex[:16], expected_pubkey_hex[:16],
        )
        return False

    # Cross-check the envelope's ``signer_pubkey_hex`` against the DID-
    # derived pubkey. They MUST agree — disagreement means the
    # envelope is internally inconsistent, even if technically both
    # fields parse.
    envelope_pk = str(receipt.get("signer_pubkey_hex", "")).lower()
    if envelope_pk and envelope_pk != pubkey_hex.lower():
        logger.debug(
            "envelope pubkey %s != DID-derived pubkey %s",
            envelope_pk[:16], pubkey_hex[:16],
        )
        return False

    # Sign input = raw 32-byte hash digest (NOT the hex string)
    digest_bytes = bytes.fromhex(claimed_hash)
    try:
        _VerifyKey(bytes.fromhex(pubkey_hex)).verify(digest_bytes, sig_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ed25519 verify failed: %s", exc)
        return False
    return True


# ─── persistent store ────────────────────────────────────────────────


class ReceiptStore:
    """File-backed receipt store at ``<workspace>/team_receipts/``.

    Each receipt is written atomically as ``{receipt_id}.json`` via
    write-temp + rename (POSIX guarantees atomicity for same-FS
    renames; Windows ``os.replace`` provides the same).

    The store is intentionally a flat directory rather than a date-
    sharded tree — for the volumes NTH DAO sees (chat-scale, not
    web-scale), the directory enumeration cost is dominated by
    serialization, not by ``readdir`` traversal. Add sharding when
    we cross 10k receipts per workspace.
    """

    SUFFIX = ".json"

    def __init__(self, workspace: Path) -> None:
        self.root = Path(workspace) / "team_receipts"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, receipt: Dict[str, Any]) -> Path:
        """Persist a signed receipt. Returns the final file path.

        Atomic: writes to ``{id}.json.tmp`` then ``os.replace``. A
        crash mid-write leaves either the old file (or no file) and
        a possibly-orphaned ``.tmp`` that's easy to spot.
        """
        rid = str(receipt.get("receipt_id", "") or "")
        if not rid:
            raise ValueError("receipt is missing receipt_id")
        # MI-1 (review fix 2026-06-08): allow only [A-Za-z0-9-]. This
        # is stricter than a path-traversal check — it incidentally
        # rejects ``..``, ``/`` and ``\`` because none of those satisfy
        # ``isalnum or '-'``, but the primary intent is "ids must be
        # plain identifiers", not "only block traversal". Document the
        # constraint accurately so a future maintainer doesn't relax
        # it thinking they're just trimming a path-traversal guard.
        if not all(c.isalnum() or c == "-" for c in rid):
            raise ValueError(
                f"receipt_id must be alphanumeric (or dash); got {rid!r}"
            )
        path = self.root / (rid + self.SUFFIX)
        tmp = path.with_suffix(self.SUFFIX + ".tmp")
        tmp.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
        return path

    def load(self, receipt_id: str) -> Optional[Dict[str, Any]]:
        """Return the receipt dict, or None if not found."""
        if not all(c.isalnum() or c == "-" for c in receipt_id):
            return None
        path = self.root / (receipt_id + self.SUFFIX)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list_ids(self) -> List[str]:
        """Enumerate stored receipt IDs (no specific order)."""
        return [p.stem for p in self.root.glob("*" + self.SUFFIX)]

    def __contains__(self, receipt_id: str) -> bool:
        return self.load(receipt_id) is not None
