"""W3C VC Data Integrity 1.0 Ed25519Signature2020 implementation.

This is the shared cryptographic layer used by IntentMandate (T-1),
CartMandate (T-2) and PaymentMandate (T-3) for sign+verify. All three
mandates have identical proof block shapes; centralising the algorithm
keeps them in lockstep and makes T-1.1 / Voss V-1 spec conformance a
single source of truth.

Spec reference
--------------
W3C VC Data Integrity 1.0 (https://www.w3.org/TR/vc-data-integrity/):

  §4.1 Transformation - canonicalise the document
  §4.2 Hashing       - hash transformed document + hash transformed
                       proof options separately
  §4.3 Proof         - signature is over the CONCATENATION
                         sha256(canonical(proof_options))
                       || sha256(canonical(document))

The previous nth_dao sign/verify covered only the document, leaving
proof.created / proof.proofPurpose / proof.verificationMethod
unauthenticated (Voss V-1). This module closes that gap.

Voss V-9 is also handled here: the verificationMethod fragment is the
multibase z-string from the issuer's did:key, not the raw hex pubkey.
For ``did:key:z6MkXyz...`` the verificationMethod becomes
``did:key:z6MkXyz...#z6MkXyz...`` which is what didkit / vc-js / the
universal resolver actually look for.

Migration note
--------------
This is a BREAKING wire change vs the v0.9.x signing scheme. Old
signatures will not verify under the new code; new signatures will
not verify under the old code. v0.10 is the explicit migration cut.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

from ..identity import AgentIdentity, canonical_json

logger = logging.getLogger("nth_dao.mandate._data_integrity")


# ===== one-shot crypto availability =====
#
# Mirrors intent.py's V-14 fix: probe at module load so the hot verify
# path doesn't pay an import lookup per call.

try:
    from ..did_key import decode_ed25519_did_key as _decode_did_key
    from nacl.signing import VerifyKey as _VerifyKey   # type: ignore[import-not-found]

    _CRYPTO_AVAILABLE = True
    _CRYPTO_IMPORT_ERROR: Optional[str] = None
except ImportError as _exc:    # pragma: no cover - env-dependent
    _decode_did_key = None      # type: ignore[assignment]
    _VerifyKey = None           # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False
    _CRYPTO_IMPORT_ERROR = str(_exc)


# Ed25519 signature byte length
_ED25519_SIG_BYTES = 64
_ED25519_SIG_HEX_CHARS = _ED25519_SIG_BYTES * 2


# ===== verificationMethod fragment (Voss V-9) =====


def verification_method(issuer_did: str) -> str:
    """Compose the VC Data Integrity verificationMethod URL.

    Per did:key spec §3.1, the fragment is the multibase z-string
    from the DID body (not the raw hex pubkey). For
    ``did:key:z6MkXyz...`` the verificationMethod is
    ``did:key:z6MkXyz...#z6MkXyz...``.

    Raises
    ------
    ValueError
        If ``issuer_did`` is not a did:key with a multibase body.
    """
    if not isinstance(issuer_did, str):
        raise ValueError(f"issuer_did must be a string, got {type(issuer_did).__name__}")
    parts = issuer_did.split(":", 2)
    if len(parts) != 3 or parts[0] != "did" or parts[1] != "key":
        raise ValueError(f"not a did:key: {issuer_did!r}")
    body = parts[2]
    if not body or not body.startswith("z"):
        raise ValueError(
            f"did:key body must be multibase z-string, got {body!r}"
        )
    return f"{issuer_did}#{body}"


# ===== sign / verify =====


def _proof_options(proof: Dict[str, Any]) -> Dict[str, Any]:
    """Return the proof block without proofValue.

    Per VC Data Integrity §4.2, these are the "proof options" that
    must be hashed and included in the signed payload alongside the
    document. Excluding proofValue is what makes the signature
    over-itself problem solvable.
    """
    return {k: v for k, v in proof.items() if k != "proofValue"}


def sign_with_data_integrity(
    *,
    identity: AgentIdentity,
    document: Dict[str, Any],
    proof_options: Dict[str, Any],
) -> str:
    """Produce an Ed25519Signature2020 proofValue per VC Data Integrity.

    Parameters
    ----------
    identity
        Signing identity (must have a private key).
    document
        The mandate dict WITHOUT its proof block. The caller already
        stripped it; this function does NOT re-strip so the caller
        retains responsibility for the canonical document shape.
    proof_options
        The proof block intended for the document, MINUS the
        ``proofValue`` field. Must include at minimum ``type``,
        ``created``, ``verificationMethod``, ``proofPurpose``.

    Returns
    -------
    str
        Lowercase 128-hex Ed25519 signature.

    Raises
    ------
    RuntimeError
        If ``identity`` cannot sign.
    ValueError
        If ``document`` or ``proof_options`` are not dicts.
    """
    if not identity.can_sign:
        raise RuntimeError("identity has no signing key - cannot sign mandate")
    if not isinstance(document, dict):
        raise ValueError("document must be a dict")
    if not isinstance(proof_options, dict):
        raise ValueError("proof_options must be a dict")
    if "proofValue" in proof_options:
        raise ValueError(
            "proof_options must NOT include proofValue - that's what we "
            "are about to compute"
        )

    doc_hash = hashlib.sha256(canonical_json(document)).digest()
    opt_hash = hashlib.sha256(canonical_json(proof_options)).digest()
    # VC Data Integrity §4.3: proof options hash || document hash
    return identity.sign(opt_hash + doc_hash).hex()


def verify_with_data_integrity(
    *,
    document: Dict[str, Any],
    proof: Dict[str, Any],
    pubkey_bytes: bytes,
) -> Tuple[bool, str]:
    """Verify an Ed25519Signature2020 proofValue per VC Data Integrity.

    Reconstructs the signed payload from ``document`` (mandate minus
    proof) + ``proof`` minus proofValue, then verifies the signature.

    Returns
    -------
    (ok, reason)
        Bool result with a forensic-actionable reason string.

    Raises
    ------
    RuntimeError
        If crypto deps unavailable at module load - the caller
        intent.py / cart.py / payment.py already raises in this
        case, but a direct caller of this helper deserves the same
        loud failure.
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError(
            f"verification requires PyNaCl and did_key support: "
            f"{_CRYPTO_IMPORT_ERROR}"
        )
    if not isinstance(proof, dict):
        return False, "proof must be a dict"
    sig_hex = proof.get("proofValue", "")
    if not isinstance(sig_hex, str) or not sig_hex:
        return False, "missing proofValue"
    if len(sig_hex) != _ED25519_SIG_HEX_CHARS:
        return False, (
            f"proofValue must be {_ED25519_SIG_HEX_CHARS}-hex Ed25519 sig, "
            f"got {len(sig_hex)} chars"
        )
    try:
        sig_bytes = bytes.fromhex(sig_hex)
    except ValueError as exc:
        return False, f"proofValue is not valid hex: {exc}"

    proof_options = _proof_options(proof)
    doc_hash = hashlib.sha256(canonical_json(document)).digest()
    opt_hash = hashlib.sha256(canonical_json(proof_options)).digest()
    try:
        _VerifyKey(pubkey_bytes).verify(opt_hash + doc_hash, sig_bytes)   # type: ignore[misc]
    except Exception as exc:    # noqa: BLE001 - nacl.exceptions.BadSignatureError
        return False, f"signature invalid: {exc}"
    return True, "ok"


def decode_issuer_pubkey(issuer_did: str) -> bytes:
    """did:key -> raw 32-byte Ed25519 pubkey.

    Wraps the did_key decoder so callers don't have to know which
    module owns it.
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError(
            f"did:key decode requires the did_key module: "
            f"{_CRYPTO_IMPORT_ERROR}"
        )
    return _decode_did_key(issuer_did)   # type: ignore[misc]


__all__ = [
    "decode_issuer_pubkey",
    "sign_with_data_integrity",
    "verify_with_data_integrity",
    "verification_method",
]
