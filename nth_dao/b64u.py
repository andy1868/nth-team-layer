"""Base64url codec helpers — RFC 4648 §5, no padding.

CR-1 (review fix 2026-06-08): consolidates the per-module ``_b64u``
helpers that had drifted into ``execution_receipt.py`` and
``a2a_card.py``. Both modules emit JWS-style and motebit-style
base64url payloads; a bug fix in one (e.g. handling of an empty
input, or a future migration from ``urlsafe_b64encode`` to a
constant-time variant) was previously not synchronised across both
sites.

The encoding is exactly the JOSE convention (RFC 7515 §2,
``BASE64URL(OCTETS)``):
  * alphabet ``-`` and ``_`` instead of ``+`` and ``/``
  * no padding (``=`` stripped from output, restored when decoding)

These helpers are intentionally pure functions — no dependencies on
anything beyond stdlib — so any layer of the codebase (identity,
receipts, A2A cards, future protocols) can adopt them without
pulling in heavier modules.
"""

from __future__ import annotations

import base64


def b64u_encode(raw: bytes) -> str:
    """Encode bytes as RFC 4648 §5 base64url with NO padding.

    Empty input is a no-op: ``b64u_encode(b"") == ""``.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    """Decode an RFC 4648 §5 base64url string (with or without padding).

    Restores the implicit ``=`` padding before decoding so callers
    don't need to remember whether the producer stripped it. Raises
    ``binascii.Error`` (subclass of ``ValueError``) on malformed
    input — characters outside the URL-safe alphabet (``A-Z``,
    ``a-z``, ``0-9``, ``-``, ``_``, ``=``) trigger a real error
    rather than silently producing empty output.

    Implementation: ``base64.urlsafe_b64decode`` is permissive and
    will quietly drop non-alphabet characters, so we use
    ``base64.b64decode`` with explicit ``altchars=b"-_"`` plus
    ``validate=True``. That's the only stdlib path that actually
    enforces the alphabet.

    Empty input is a no-op: ``b64u_decode("") == b""``.
    """
    if not s:
        return b""
    padded = s + "=" * (-len(s) % 4)
    return base64.b64decode(
        padded.encode("ascii"), altchars=b"-_", validate=True,
    )
