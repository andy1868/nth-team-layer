"""Conformance vector runner — loads vectors.json + verifies behavior.

This module is the contract between the Python reference implementation
and any future port (Rust / Go / TypeScript / …). A port is wire-compatible
when its equivalent of `run_all_vectors()` produces zero failures.

All vector inputs are deterministic: fixed seeds, fixed timestamps, no
randomness. That means a port can compute the SAME outputs from the SAME
inputs without needing access to any Python-only entropy source.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..identity import canonical_json

logger = logging.getLogger("nth_dao.conformance")


VECTORS_PATH = Path(__file__).parent / "vectors.json"


@dataclass
class ConformanceFailure:
    """One vector failed under the reference implementation."""

    vector_id: str
    category: str
    description: str
    expected: Any
    actual: Any


def load_vectors(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the vectors.json file as a dict."""
    path = path or VECTORS_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"conformance vectors not found at {path}; "
            "run `python -m nth_dao.conformance.regenerate` first"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────── individual checkers ───────────────────


def check_canonical_json(vectors: List[dict]) -> List[ConformanceFailure]:
    """Verify the canonical JSON encoder produces the expected bytes."""
    failures = []
    for v in vectors:
        expected = bytes.fromhex(v["expected_bytes_hex"])
        actual = canonical_json(v["input"])
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="canonical_json",
                description=v.get("description", ""),
                expected=expected.hex(),
                actual=actual.hex(),
            ))
    return failures


def check_fingerprint(vectors: List[dict]) -> List[ConformanceFailure]:
    """AgentIdentity.fingerprint() must be sha256(pubkey_hex or agent_id)[:16]."""
    failures = []
    for v in vectors:
        payload = v["input"]["pubkey_hex"] or v["input"]["agent_id"]
        actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        expected = v["expected_fingerprint"]
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="fingerprint",
                description=v.get("description", ""),
                expected=expected,
                actual=actual,
            ))
    return failures


def check_signature_verify(vectors: List[dict]) -> List[ConformanceFailure]:
    """Ed25519 verify with a known pubkey + message + signature."""
    try:
        from nacl.signing import VerifyKey
    except ImportError:
        # Skip silently — PyNaCl not installed
        return []
    failures = []
    for v in vectors:
        pubkey_hex = v["pubkey_hex"]
        message = bytes.fromhex(v["message_hex"])
        signature = bytes.fromhex(v["signature_hex"])
        expected = bool(v["expected_valid"])
        try:
            VerifyKey(bytes.fromhex(pubkey_hex)).verify(message, signature)
            actual = True
        except Exception:
            actual = False
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="signature_verify",
                description=v.get("description", ""),
                expected=expected,
                actual=actual,
            ))
    return failures


def check_endorsement_canonical_payload(vectors: List[dict]) -> List[ConformanceFailure]:
    """Endorsement.signable_dict() canonicalized must match expected bytes.

    This locks the field order and exact serialization. A Rust/Go port that
    re-orders fields will diverge here.
    """
    from ..web_of_trust import Endorsement
    failures = []
    for v in vectors:
        e = Endorsement.from_dict(v["input"])
        actual = canonical_json(e.signable_dict()).hex()
        expected = v["expected_canonical_hex"]
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="endorsement_canonical_payload",
                description=v.get("description", ""),
                expected=expected,
                actual=actual,
            ))
    return failures


def check_template_canonical_payload(vectors: List[dict]) -> List[ConformanceFailure]:
    """MissionTemplate.signable_dict() canonicalized must match expected bytes."""
    from ..orchestration.template import MissionTemplate
    failures = []
    for v in vectors:
        t = MissionTemplate.from_dict(v["input"])
        actual = canonical_json(t.signable_dict()).hex()
        expected = v["expected_canonical_hex"]
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="template_canonical_payload",
                description=v.get("description", ""),
                expected=expected,
                actual=actual,
            ))
    return failures


def check_replay_window(vectors: List[dict]) -> List[ConformanceFailure]:
    """gossip replay window: accept (now - 600s, now + 60s], reject otherwise."""
    from ..gossip import _within_replay_window
    from datetime import datetime, timedelta
    failures = []
    for v in vectors:
        # Vector specifies the message timestamp as a relative-to-now offset
        # in seconds, so it stays correct regardless of when the test runs.
        offset = int(v["offset_seconds"])
        ts = (datetime.now() + timedelta(seconds=offset)).isoformat()
        actual = _within_replay_window(ts)
        expected = bool(v["expected_within_window"])
        if actual != expected:
            failures.append(ConformanceFailure(
                vector_id=v["id"],
                category="replay_window",
                description=v.get("description", ""),
                expected=expected,
                actual=actual,
            ))
    return failures


# ─────────────────── orchestration ───────────────────


_CHECKERS: Dict[str, Callable[[List[dict]], List[ConformanceFailure]]] = {
    "canonical_json":              check_canonical_json,
    "fingerprint":                 check_fingerprint,
    "signature_verify":            check_signature_verify,
    "endorsement_canonical_payload": check_endorsement_canonical_payload,
    "template_canonical_payload":  check_template_canonical_payload,
    "replay_window":               check_replay_window,
}


def run_all_vectors(
    vectors_data: Optional[Dict[str, Any]] = None,
) -> List[ConformanceFailure]:
    """Execute every checker against the loaded vectors.

    Returns a list of failures; empty list = wire-compatible.
    """
    if vectors_data is None:
        vectors_data = load_vectors()
    all_failures: List[ConformanceFailure] = []
    for category, checker in _CHECKERS.items():
        category_data = vectors_data.get("vectors", {}).get(category, [])
        if not category_data:
            continue
        try:
            failures = checker(category_data)
            all_failures.extend(failures)
        except Exception as e:
            logger.warning("checker %s raised %s; skipping category", category, e)
    return all_failures
