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
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..util import atomic_write_json, safe_load_json
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
        if not isinstance(mandate, dict):
            raise ValueError(f"{kind} mandate must be a dict")
        digest = digest_fn(mandate)
        path = self._path(kind, digest)
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
        out: List[Dict[str, Any]] = []
        for path in sorted((self.root / kind).glob("*.json")):
            data = safe_load_json(path, fallback=None)
            if isinstance(data, dict):
                out.append(data)
            else:
                logger.warning("dropping corrupt mandate at %s", path)
        return out

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
