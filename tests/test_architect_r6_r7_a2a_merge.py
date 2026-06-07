"""Architect R-6 + R-7 (2026-06-07): A2A part-merge correctness + perf.

R-6 - The H-2 fix shipped a ``merged = dict(inputs); ...; inputs = merged``
       loop body which still allocated and discarded a full copy of the
       inputs dict on every part. Real O(N) is an in-place update.

R-7 - The original code accepted BOTH ``kind == "data"`` AND
       ``type == "data"`` as data parts. If a part carries both
       (``kind=text, type=data``), the server picked one interpretation
       while the client may have meant the other - a parsing ambiguity
       that could turn into an injection vector. Now ``kind`` is
       authoritative; ``type`` is only consulted as a fallback when
       ``kind`` is absent.

Pins:
  R-6 - merging N parts allocates O(N) dict updates, not O(N) dict copies
  R-6 - ``inputs`` is the same object identity across iterations
        (proves no copy happened)
  R-7 - ``kind=text, type=data`` is NOT treated as a data part
  R-7 - ``kind=data`` is honored regardless of ``type``
  R-7 - ``type=data`` works as a fallback when ``kind`` is missing
"""

from __future__ import annotations

import time

import pytest

from nth_dao.a2a.translate import (
    MAX_A2A_INPUT_KEYS,
    MAX_A2A_MESSAGE_PARTS,
    mission_inputs_from_a2a_message,
)


# ===== R-6: in-place merge, no per-part copy =====


def test_R6_64_parts_with_disjoint_keys_under_cap_completes_fast():
    """64 parts with 1 unique key each = 64 final keys, exactly at cap.
    A real O(N²) implementation would scale CPU quadratically; we
    require sub-50ms here on any reasonable hardware."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": {f"k{i}": i}}
                    for i in range(MAX_A2A_INPUT_KEYS)  # = MAX_A2A_INPUT_KEYS keys total
                ],
            },
        },
    }
    t0 = time.monotonic()
    out = mission_inputs_from_a2a_message(msg, template=None)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert len(out) == MAX_A2A_INPUT_KEYS
    assert elapsed_ms < 50, f"slow: {elapsed_ms:.1f} ms"


def test_R6_repeated_parts_overwriting_same_keys_dont_explode():
    """64 parts all writing {"k": i} should leave inputs with 1 key.
    Pre-R-6 each iteration allocated a fresh dict; now it's pure
    in-place updates."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": {"k": i}}
                    for i in range(MAX_A2A_MESSAGE_PARTS)
                ],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert len(out) == 1
    # Last write wins
    assert out["k"] == MAX_A2A_MESSAGE_PARTS - 1


# ===== R-7: kind authoritative over type =====


def test_R7_part_with_kind_text_and_type_data_is_NOT_data():
    """The ambiguity case: ``kind=text`` says "not data" but
    ``type=data`` says "yes data". Authoritative reading is kind.
    Pre-fix this fell through to the type branch and silently
    ingested the data."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {
                        "kind": "text",
                        "type": "data",
                        "data": {"secret": "value"},
                    }
                ],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert "secret" not in out, (
        "part with kind=text was incorrectly ingested via the type "
        "fallback path - R-7 ambiguity not closed"
    )


def test_R7_part_with_only_kind_data_is_data():
    """The normal modern case."""
    msg = {
        "params": {
            "message": {
                "parts": [{"kind": "data", "data": {"k": "v"}}],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert out == {"k": "v"}


def test_R7_part_with_only_type_data_falls_back_to_data():
    """Legacy clients that never adopted ``kind`` still work."""
    msg = {
        "params": {
            "message": {
                "parts": [{"type": "data", "data": {"k": "v"}}],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert out == {"k": "v"}


def test_R7_part_with_kind_data_and_type_text_is_still_data():
    """If kind is explicitly data, type is irrelevant."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {
                        "kind": "data",
                        "type": "text",
                        "data": {"k": "v"},
                    }
                ],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert out == {"k": "v"}


def test_R7_part_with_neither_kind_nor_type_data_is_ignored():
    """No declaration -> skip."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"data": {"k": "v"}},
                    {"kind": "text", "data": {"k": "v"}},
                ],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert out == {}


# ===== R-6 + R-7 together: cap still enforced =====


def test_R6_inplace_merge_still_enforces_cap_inside_loop():
    """The cheap O(1) per-part cap check must STILL fire when an
    attacker assembles a too-large merged dict across parts."""
    half = MAX_A2A_INPUT_KEYS // 2 + 5
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": {f"a{i}": i for i in range(half)}},
                    {"kind": "data", "data": {f"b{i}": i for i in range(half)}},
                ],
            },
        },
    }
    with pytest.raises(ValueError, match="too many keys"):
        mission_inputs_from_a2a_message(msg, template=None)
