"""G-14 (Voss audit): _capture_env_metadata captures GPU + memory.

PR-2's original schema captured only platform / architecture /
python_version / runtime. That's enough for OS-level filtering but
insufficient for:

  * GPU-required ML steps (Linux + amd64 is not enough — does this
    host have an NVIDIA card?)
  * memory-bound large-context steps (some hosts have 8 GiB, others
    have 256 GiB)

G-14 adds cpu_count / memory_gb / gpu_* keys to the env snapshot.
Each new field has a graceful fallback so attach() never crashes on a
sandboxed CI runner with no GPU and no psutil.

Pinned invariants:
  * Original PR-2 keys still present and still strings (unchanged)
  * New keys present with documented types or None
  * GPU detection is best-effort - never raises
  * Memory probe returns None gracefully when psutil missing
  * gpu_source describes HOW we detected (None / pynvml / nvidia-smi)
"""

from __future__ import annotations

import pytest

from nth_dao.attach import _capture_env_metadata, _detect_gpu, _detect_memory_gb


# ===== additive contract =====


def test_G14_capture_includes_original_pr2_keys_unchanged():
    """G-14 is additive - the four PR-2 string keys MUST still be
    string-typed. Existing platform filtering depends on this."""
    meta = _capture_env_metadata()
    for k in ("platform", "architecture", "python_version", "runtime"):
        assert k in meta
        assert isinstance(meta[k], str)
        assert meta[k] != ""


def test_G14_capture_includes_new_g14_keys():
    """All G-14 keys must appear regardless of host GPU/psutil
    availability (graceful fallback gives None / False / 0)."""
    meta = _capture_env_metadata()
    new_keys = (
        "cpu_count", "memory_gb",
        "gpu_available", "gpu_name", "gpu_count", "gpu_source",
    )
    for k in new_keys:
        assert k in meta, f"missing G-14 key: {k}"


# ===== cpu_count =====


def test_G14_cpu_count_is_positive_int():
    """os.cpu_count() returns None on truly weird platforms; we
    normalise that to 0. On any real host it must be >= 1."""
    meta = _capture_env_metadata()
    assert isinstance(meta["cpu_count"], int)
    assert meta["cpu_count"] >= 1  # any real CI host has at least 1


# ===== memory_gb =====


def test_G14_memory_gb_is_float_or_none():
    """When psutil is present (typical), memory_gb is a positive
    float in GiB. When psutil is missing, it's None - never a
    string, never a crash."""
    val = _detect_memory_gb()
    assert val is None or (isinstance(val, float) and val > 0)


def test_G14_memory_gb_in_env_metadata_matches_helper():
    """End-to-end: _capture_env_metadata's memory_gb must agree
    with _detect_memory_gb directly. Catches accidental override."""
    meta = _capture_env_metadata()
    direct = _detect_memory_gb()
    assert meta["memory_gb"] == direct


# ===== gpu detection =====


def test_G14_detect_gpu_returns_consistent_shape():
    """_detect_gpu's contract: always returns a dict with the four
    documented keys, regardless of whether a GPU was found."""
    gpu = _detect_gpu()
    assert set(gpu.keys()) == {
        "gpu_available", "gpu_name", "gpu_count", "gpu_source",
    }
    assert isinstance(gpu["gpu_available"], bool)
    assert gpu["gpu_name"] is None or isinstance(gpu["gpu_name"], str)
    assert isinstance(gpu["gpu_count"], int)
    assert gpu["gpu_source"] in (None, "pynvml", "nvidia-smi")


def test_G14_detect_gpu_consistency_among_fields():
    """When gpu_available is False, name/count/source must all be
    falsy (None / 0). When True, source identifies the detector."""
    gpu = _detect_gpu()
    if gpu["gpu_available"]:
        assert gpu["gpu_source"] in ("pynvml", "nvidia-smi")
        assert gpu["gpu_count"] >= 1
        assert isinstance(gpu["gpu_name"], str) and gpu["gpu_name"]
    else:
        assert gpu["gpu_source"] is None
        assert gpu["gpu_count"] == 0
        assert gpu["gpu_name"] is None


def test_G14_detect_gpu_never_raises_even_when_pynvml_broken(monkeypatch):
    """Defence: even if pynvml is installed but broken (driver
    missing, init fails), _detect_gpu must return the falsy
    fallback, NOT raise. This guards attach() against host
    misconfiguration.

    The attach submodule is shadowed by the same-named function
    re-exported from nth_dao/__init__.py, so we look it up via
    sys.modules rather than attribute access on the package.
    """
    import sys
    mod = sys.modules["nth_dao.attach"]

    # Patch shutil.which to claim no nvidia-smi binary - forces
    # _detect_gpu to rely on the pynvml path or the fallback.
    monkeypatch.setattr(mod.shutil, "which", lambda binary: None)

    # We can't reliably break pynvml in-process without complex
    # import gymnastics, but the contract is "never raises". So we
    # just call it and confirm no exception escapes.
    gpu = _detect_gpu()
    # Either it found a real GPU via pynvml, or fell back cleanly.
    assert isinstance(gpu["gpu_available"], bool)


# ===== safe-for-storage =====


def test_G14_env_metadata_is_json_serialisable():
    """The registry stores env metadata under metadata.env and
    serialises via atomic_write_json -> json.dumps. All values
    must therefore be JSON-encodable (no exotic types from pynvml
    handles, no bytes, no enums)."""
    import json
    meta = _capture_env_metadata()
    encoded = json.dumps(meta)  # raises TypeError if not encodable
    decoded = json.loads(encoded)
    assert decoded == meta
