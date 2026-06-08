"""Canonical JSON encoding — the wire-format primitive for every
signed object in NTH DAO.

MI-3 (review fix 2026-06-08): this module previously lived inside
``nth_dao/identity.py``. That was a smell: ``canonical_json`` is a
domain-free serializer, used by mandates, group registries, identity
cards, A2A cards, execution receipts, gossip records, etc. — but
every consumer had to ``from nth_dao.identity import canonical_json``,
which makes the dependency direction unintuitive and risks a future
``identity.py`` refactor accidentally breaking serialization for
unrelated subsystems.

Bytes contract (DO NOT CHANGE without a wire-format major bump):

  * Object keys sorted lexicographically at every level
  * No whitespace (``separators=(",", ":")``)
  * UTF-8 encoded, ``ensure_ascii=False`` so non-ASCII strings round-trip
  * NaN / Inf rejected (``allow_nan=False``)
  * Floats rejected outright at the validator — wire payloads must
    represent fractional quantities as int (with documented scale) or
    decimal strings; this is the same rule motebit uses

Every signed artifact in the codebase depends on this exact byte
output. Two implementations of the same spec MUST agree byte-for-byte
or signatures stop verifying across the network.

The legacy ``nth_dao.identity`` module still re-exports these names
for backwards compatibility — existing imports work unchanged.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict


def _validate_canonical_json_value(value: Any, path: str = "$") -> None:
    """Walk a value tree and raise on anything ``canonical_json`` won't
    accept. Float, set, bytes, custom classes — all rejected.

    Raises ``TypeError`` with a JSON-Path-style location so the caller
    knows which key inside a nested payload tripped the check.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise TypeError(
            f"canonical_json rejects float at {path}; use int or "
            f"decimal string"
        )
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_canonical_json_value(item, f"{path}[{idx}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"canonical_json object keys must be strings at "
                    f"{path}; got {type(key).__name__}"
                )
            _validate_canonical_json_value(item, f"{path}.{key}")
        return
    raise TypeError(
        f"canonical_json does not support {type(value).__name__} "
        f"at {path}"
    )


def normalize_for_canonical_json(value: Any, path: str = "$") -> Any:
    """Return a JSON value whose numbers are stable across implementations.

    This is for internal wire objects that historically carried floats
    in event payloads. The public ``canonical_json`` API remains
    strict and will reject floats; callers must opt in to this
    normalization explicitly.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        text = str(value)
        try:
            decimal = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid float at {path}") from exc
        if not decimal.is_finite():
            raise ValueError(f"non-finite float at {path}")
        return format(decimal, "f")
    if isinstance(value, list):
        return [
            normalize_for_canonical_json(item, f"{path}[{idx}]")
            for idx, item in enumerate(value)
        ]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"canonical JSON object keys must be strings at "
                    f"{path}; got {type(key).__name__}"
                )
            out[key] = normalize_for_canonical_json(item, f"{path}.{key}")
        return out
    raise TypeError(
        f"cannot normalize {type(value).__name__} for canonical "
        f"JSON at {path}"
    )


def canonical_json(data: Dict[str, Any]) -> bytes:
    """Serialize ``data`` to canonical-form UTF-8 bytes.

    Spec recipe (locked across the NTH DAO ecosystem; bytes match
    motebit, mandate signatures, group registry sigs, etc.):

      * Root must be a dict (a top-level array would be ambiguous
        for verifiers that build their own canonical form).
      * Validate the value tree — float / bytes / sets / non-string
        keys all raise TypeError early so the producer fails loudly
        instead of silently emitting a wire format no verifier
        accepts.
      * ``json.dumps`` with ``sort_keys=True``, no whitespace,
        ``ensure_ascii=False``, ``allow_nan=False``.
      * Encode UTF-8.

    Raises:
        TypeError if the root isn't a dict, or if any value in the
        tree is of an unsupported type.
    """
    if not isinstance(data, dict):
        raise TypeError(
            f"canonical_json root must be a dict, got "
            f"{type(data).__name__}"
        )
    _validate_canonical_json_value(data)
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
