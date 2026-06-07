"""Architect audit H-2 + H-3 (2026-06-07): A2A translate.py corrections.

H-2: mission_inputs_from_a2a_message originally called the expensive
``_check_a2a_input_bounds`` (full json.dumps + recursive walk) TWICE per
message part. With 64 parts that meant 128 deep checks, O(N^2) in part
count. Fix: only enforce the cheap O(1) merged-key-count cap inside the
loop, defer the deep bounds check until after the merge.

H-3: ``_is_supported_agent_did`` had ``from nth_dao.did_key import is_did_key``
inside the function body, hitting Python's import system on every DID
validation. Hoisted to module scope.

Pinned invariants:
  H-2:
    * 64 parts with a normal payload completes in a single-digit ms
    * call count to _check_a2a_input_bounds is now exactly 1 per request
    * a merged dict exceeding MAX_A2A_INPUT_KEYS is still rejected
      (so the cheap in-loop check is still effective)
  H-3:
    * _is_supported_agent_did does not contain ``import`` in its source
    * Module-scope ``is_did_key`` is importable
"""

from __future__ import annotations

import inspect
import time

import pytest

from nth_dao.a2a import translate as translate_mod
from nth_dao.a2a.translate import (
    MAX_A2A_INPUT_KEYS,
    MAX_A2A_MESSAGE_PARTS,
    _is_supported_agent_did,
    mission_inputs_from_a2a_message,
)


# ===== H-2: bounds check called once =====


def test_H2_bounds_check_called_exactly_once_per_message(monkeypatch):
    """Pre-fix: 2N calls. Post-fix: exactly 1 call regardless of N parts."""
    calls = []
    real_check = translate_mod._check_a2a_input_bounds

    def counting_check(inputs):
        calls.append(len(inputs))
        return real_check(inputs)

    monkeypatch.setattr(
        translate_mod, "_check_a2a_input_bounds", counting_check,
    )

    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": {f"k{i}": i}}
                    for i in range(20)
                ],
            },
        },
    }
    result = mission_inputs_from_a2a_message(msg, template=None)
    assert len(result) == 20
    # Exactly one deep check, post-merge
    assert len(calls) == 1, (
        f"_check_a2a_input_bounds called {len(calls)} times; "
        f"should be exactly 1 per request"
    )


def test_H2_64_parts_completes_in_reasonable_time():
    """Performance assertion: a max-sized batch (64 parts, each with
    the SAME key namespace so the merge stays under cap) must complete
    in well under 100ms. Pre-fix this was O(N^2) in parts and could
    spike to seconds with adversarial data."""
    # Same 10 keys in every part - merge stays at 10 keys regardless
    # of part count.
    shared_keys = {f"k{j}": j for j in range(10)}
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": dict(shared_keys)}
                    for _ in range(MAX_A2A_MESSAGE_PARTS)
                ],
            },
        },
    }
    t0 = time.monotonic()
    result = mission_inputs_from_a2a_message(msg, template=None)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 100, f"slow path: {elapsed_ms:.1f} ms"
    assert len(result) == 10


# ===== H-2: cheap in-loop cap still effective =====


def test_H2_merged_keys_over_cap_rejected_inside_loop():
    """The cheap O(1) per-part `len(merged) > MAX_A2A_INPUT_KEYS`
    check must STILL fire when an attacker assembles a too-large
    merged dict across parts. Otherwise the deferred deep check could
    be reached with an unbounded dict."""
    # Split a >cap key set across multiple parts so no single part is
    # over-cap, but the merge would be.
    half = MAX_A2A_INPUT_KEYS // 2 + 5  # ensure each part is just under cap
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


# ===== H-2: single-part still works =====


def test_H2_single_part_unchanged_behaviour():
    """No regression for the common one-part case."""
    msg = {
        "params": {
            "message": {
                "parts": [
                    {"kind": "data", "data": {"goal": "x", "limit": 5}},
                ],
            },
        },
    }
    out = mission_inputs_from_a2a_message(msg, template=None)
    assert out == {"goal": "x", "limit": 5}


# ===== H-3: no inline import =====


def test_H3_is_supported_agent_did_does_not_import_inline():
    """The lazy-import is gone - if it crept back in via merge, this
    fails. Mirrors the G-9 pattern that pinned backend preflight."""
    src = inspect.getsource(_is_supported_agent_did)
    # Walk source lines; ignore the def header and any docstring lines
    body_lines = src.split("\n")[1:]
    in_docstring = False
    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith('"""'):
            in_docstring = not in_docstring or not stripped.endswith('"""', 3)
            continue
        if in_docstring:
            continue
        # Forbid bare imports inside the function body
        assert not stripped.startswith("from "), (
            f"_is_supported_agent_did contains inline 'from' import: {stripped!r}"
        )
        assert not stripped.startswith("import "), (
            f"_is_supported_agent_did contains inline 'import' statement: {stripped!r}"
        )


def test_H3_is_did_key_imported_at_module_scope():
    """The hoisted import must actually resolve - regression for the
    'forgot to add the import after removing the lazy one' bug."""
    assert hasattr(translate_mod, "is_did_key")
    assert callable(translate_mod.is_did_key)


def test_H3_is_supported_agent_did_still_works():
    """Behavioural smoke: a real did:key still validates."""
    # Known-valid Ed25519 did:key sample
    sample = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"
    assert _is_supported_agent_did(sample)
    # Invalid: random string
    assert not _is_supported_agent_did("not-a-did")
    # did:web with non-empty host
    assert _is_supported_agent_did("did:web:example.com")
    # did:web with empty host should fail
    assert not _is_supported_agent_did("did:web:")
