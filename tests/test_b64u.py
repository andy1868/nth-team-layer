"""nth_dao.b64u — shared base64url codec (CR-1 fix, 2026-06-08).

What this suite proves:

  1. Encode strips ``=`` padding and uses the URL-safe alphabet
     (``-``/``_`` not ``+``/``/``).
  2. Decode restores padding implicitly so callers don't need to
     remember whether the producer stripped it.
  3. Empty input round-trips cleanly (no exceptions, no surprises).
  4. Random round-trips are stable.
  5. Decode rejects truly malformed input.
"""

from __future__ import annotations

import os

import pytest

from nth_dao.b64u import b64u_decode, b64u_encode


def test_encode_strips_padding():
    # 1 byte → 2 base64 chars + 2 "=" padding → 2 chars after strip
    assert b64u_encode(b"\x00") == "AA"
    # 2 bytes → 3 base64 chars + 1 "=" padding → 3 chars
    assert b64u_encode(b"\x00\x00") == "AAA"
    # 3 bytes → 4 base64 chars + 0 "=" padding → 4 chars
    assert b64u_encode(b"\x00\x00\x00") == "AAAA"


def test_encode_uses_url_safe_alphabet():
    """Standard b64 would emit ``+`` and ``/`` for these bytes; b64url
    must emit ``-`` and ``_``."""
    raw = b"\xff\xff\xff"      # → standard "////" → b64url "____"
    assert b64u_encode(raw) == "____"
    raw = b"\xfb\xff\xff"      # → standard "+///" → b64url "-___"
    assert b64u_encode(raw) == "-___"


def test_decode_restores_padding_implicitly():
    """A producer that stripped padding can be decoded without the
    consumer reattaching ``=`` itself."""
    assert b64u_decode("AA") == b"\x00"
    assert b64u_decode("AAA") == b"\x00\x00"
    assert b64u_decode("AAAA") == b"\x00\x00\x00"


def test_decode_accepts_already_padded_input_too():
    """Producers that DIDN'T strip padding still parse correctly."""
    assert b64u_decode("AA==") == b"\x00"
    assert b64u_decode("AAA=") == b"\x00\x00"


def test_empty_input_roundtrips():
    """Empty input must NOT raise — both directions are no-ops."""
    assert b64u_encode(b"") == ""
    assert b64u_decode("") == b""


def test_random_bytes_roundtrip():
    """For 32 random samples, encode→decode is the identity."""
    for _ in range(32):
        raw = os.urandom(64)
        assert b64u_decode(b64u_encode(raw)) == raw


def test_decode_rejects_garbage():
    """Truly malformed input (non-alphabet chars, wrong length) should
    raise a ``ValueError`` subclass — the caller can catch broadly."""
    with pytest.raises(ValueError):
        b64u_decode("!!!")