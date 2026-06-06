"""MandateStore - file-backed persistence for the Mandate triad.

Each Mandate body is stored as JSON at::

    <workspace>/mandates/<kind>/<digest>.json

where ``kind`` is ``intent`` / ``cart`` / ``payment`` and ``digest`` is
the SHA-256 hex of the canonical-JSON minus the proof block (same
digest as ``intent_mandate_digest`` / ``cart_mandate_digest`` /
``payment_mandate_digest``).

Why a file store rather than just EventBus?
    The T-5 EventBus payloads carry only digest + filterable summary
    fields - small enough that an audit dashboard can list them
    cheaply. But the FULL Mandate body (including the proof) is what
    a settlement adapter or external verifier needs. Keeping the
    bodies on disk under their digest gives an O(1) lookup keyed on
    the value the EventBus payload already carries.

Why digest as the filename?
    A re-sign of the same Mandate produces the same digest (signing
    only adds a proof block, which is excluded from the digest). So
    overwriting under the digest is idempotent - replaying an event
    stream into a fresh store produces the same on-disk state.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..util import InterProcessLock, atomic_write_json, safe_load_json
from .cart import cart_mandate_digest
from .intent import intent_mandate_digest
from .payment import payment_mandate_digest

logger = logging.getLogger("nth_dao.mandate.store")


KIND_INTENT = "intent"
KIND_CART = "cart"
KIND_PAYMENT = "payment"
KINDS = (KIND_INTENT, KIND_CART, KIND_PAYMENT)


class MandateStore:
    """Read/write the full Mandate bodies for a workspace."""

    DEFAULT_DIR = "mandates"

    def __init__(self, workspace: Union[str, Path]):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / self.DEFAULT_DIR
        for kind in KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=True)

    # ===== save =====

    def save_intent(self, mandate: Dict[str, Any]) -> str:
        return self._save(KIND_INTENT, mandate, intent_mandate_digest)

    def save_cart(self, mandate: Dict[str, Any]) -> str:
        return self._save(KIND_CART, mandate, cart_mandate_digest)

    def save_payment(self, mandate: Dict[str, Any]) -> str:
        return self._save(KIND_PAYMENT, mandate, payment_mandate_digest)

    def _save(self, kind: str, mandate: Dict[str, Any], digest_fn) -> str:
        """Persist ``mandate`` under its canonical digest filename.

        Voss V-36: replay-preserving overwrite semantics.
            Re-saving the exact same bytes is a no-op. Re-saving a
            mandate that has the same canonical-JSON-minus-proof
            digest but a DIFFERENT proof block (e.g. resigned with a
            new ``proof.created`` timestamp) preserves the
            ORIGINAL on-disk copy and logs at INFO. Otherwise an
            attacker who got hold of the mandate body and resigned
            it (using the same key but a different timestamp) could
            silently rewrite the original on-disk proof and lose
            its forensic provenance.

        Returns the digest as the authoritative storage key.
        """
        if not isinstance(mandate, dict):
            raise ValueError(f"{kind} mandate must be a dict")
        digest = digest_fn(mandate)
        path = self._path(kind, digest)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with InterProcessLock(lock_path):
            if path.exists():
                existing = safe_load_json(path, fallback=None)
                if isinstance(existing, dict):
                    if existing == mandate:
                        # Idempotent re-save - same bytes, no-op.
                        return digest
                    logger.info(
                        "%s mandate %s already stored with a different proof; "
                        "preserving the earlier copy on disk (Voss V-36)",
                        kind, digest,
                    )
                    return digest
                self._relocate_corrupt(path)
            atomic_write_json(path, mandate)
        return digest

    # ===== read =====

    def list_intents(self) -> List[Dict[str, Any]]:
        return self._list(KIND_INTENT)

    def list_carts(self) -> List[Dict[str, Any]]:
        return self._list(KIND_CART)

    def list_payments(self) -> List[Dict[str, Any]]:
        return self._list(KIND_PAYMENT)

    def _list(self, kind: str) -> List[Dict[str, Any]]:
        """List all mandates of ``kind`` from disk.

        Voss V-42: corrupt files are MOVED ASIDE rather than silently
        dropped. Without relocation, every subsequent list call
        re-scans the corrupt file, logs the same warning, and an
        operator has no way to triage. After relocation, the file
        lands at ``<digest>.json.corrupt.<unix_ts>`` adjacent to the
        valid mandates - visible to ops, invisible to listing.
        """
        out: List[Dict[str, Any]] = []
        for path in sorted((self.root / kind).glob("*.json")):
            data = safe_load_json(path, fallback=None)
            if isinstance(data, dict):
                out.append(data)
            else:
                self._relocate_corrupt(path)
        return out

    @staticmethod
    def _relocate_corrupt(path: Path) -> None:
        """Move a corrupt mandate file to a .corrupt.<ts> suffix so
        subsequent listings don't keep tripping over it."""
        try:
            relocated = path.with_suffix(
                path.suffix + f".corrupt.{int(time.time())}"
            )
            path.rename(relocated)
            logger.warning(
                "relocated corrupt mandate %s -> %s (visible to ops)",
                path.name, relocated.name,
            )
        except OSError as exc:    # pragma: no cover - filesystem-dependent
            # If relocation fails (permissions, filesystem readonly,
            # etc.) fall back to the old log-and-skip behaviour so
            # listings don't crash.
            logger.error(
                "could not relocate corrupt mandate %s: %s (will retry "
                "on next list)", path.name, exc,
            )

    # ===== lookup =====

    def get(self, kind: str, digest: str) -> Optional[Dict[str, Any]]:
        if kind not in KINDS:
            raise ValueError(f"unknown mandate kind: {kind!r}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(
                f"digest must be 64-hex SHA-256, got {digest!r}"
            )
        path = self._path(kind, digest)
        if not path.exists():
            return None
        data = safe_load_json(path, fallback=None)
        return data if isinstance(data, dict) else None

    def _path(self, kind: str, digest: str) -> Path:
        return self.root / kind / f"{digest}.json"


__all__ = [
    "MandateStore",
    "KIND_INTENT",
    "KIND_CART",
    "KIND_PAYMENT",
    "KINDS",
]
