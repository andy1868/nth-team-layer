"""Hardening tests for nth_dao.agent_profile per Voss review.

H-9: as_did() soft-fails for non-Ed25519 keys instead of crashing the
whole profile build.
M-2: Markdown injection via agent_id / label / groups is escaped.
M-4: is_alive defaults False for profiles with no record source.
"""

from __future__ import annotations

import pytest

from nth_dao.agent_profile import (
    AgentProfile,
    _escape_md,
    _escape_md_code,
)


# ─── H-9: as_did soft fail ───────────────────────────────────────────


def test_H9_as_did_failure_does_not_break_profile_build():
    """A non-Ed25519 identity whose as_did() raises should leave did=""
    rather than crashing the whole AgentProfile.build()."""
    class RSAIdentity:
        label = "RSA Bot"
        pubkey_hex = "deadbeef" * 8
        def as_did(self):
            raise ValueError("did:key supports only Ed25519")
    p = AgentProfile.build("rsa-agent", identity=RSAIdentity())   # type: ignore[arg-type]
    assert p.label == "RSA Bot"
    assert p.pubkey_fingerprint == "deadbeef" * 8
    assert p.did == ""   # soft-failed, not crashed


def test_H9_as_did_NotImplementedError_handled():
    class FutureIdentity:
        label = "?"
        pubkey_hex = "ab" * 32
        def as_did(self):
            raise NotImplementedError("DID not yet supported for this curve")
    p = AgentProfile.build("a", identity=FutureIdentity())   # type: ignore[arg-type]
    assert p.did == ""


def test_H9_unexpected_exception_still_propagates():
    """Soft-fail covers EXPECTED failures (ValueError, NotImplementedError,
    AttributeError). A genuine bug (RuntimeError) should still surface
    rather than be silently swallowed."""
    class BrokenIdentity:
        label = ""
        pubkey_hex = "ab" * 32
        def as_did(self):
            raise RuntimeError("genuine bug in DID computation")
    with pytest.raises(RuntimeError):
        AgentProfile.build("a", identity=BrokenIdentity())   # type: ignore[arg-type]


# ─── M-2: markdown injection escaping ────────────────────────────────


def test_M2_pipes_in_agent_id_escaped():
    p = AgentProfile(agent_id="a|b|c", label="evil|label")
    md = p.render_markdown()
    # Pipes in cells are escaped — the table structure stays intact
    assert "\\|" in md
    # Header pipe count is unchanged
    header_line = next(line for line in md.splitlines() if line.startswith("| Field"))
    assert header_line.count("|") == 3   # opening, separator, closing


def test_M2_backticks_in_code_cell_neutralised():
    """A backtick in agent_id would close the inline code and let the
    rest of the cell be parsed as Markdown — potential injection."""
    p = AgentProfile(agent_id="alice`echo hacked`")
    md = p.render_markdown()
    # The dangerous backtick is replaced before Markdown sees the
    # inline-code cell, so it cannot close `...` syntax.
    assert "`echo hacked`" not in md
    assert "'echo hacked'" in md


def test_M2_backticks_in_cjk_label_handled():
    p = AgentProfile(label="爱丽丝`恶意`", agent_id="alice")
    md = p.render_markdown()
    # CJK label is rendered, backticks within escaped
    assert "爱丽丝" in md
    assert "`恶意`" not in md or "\\`恶意" in md   # either escape style


# ─── M-4: is_alive default for unknown agents ───────────────────────


def test_M4_unknown_agent_defaults_to_offline():
    """A profile with no record source must not assert liveness."""
    p = AgentProfile.build("unknown")
    assert p.is_alive is False


def test_M4_record_source_can_set_true():
    """A real record CAN flip is_alive back to True."""
    class FakeRecord:
        agent_id = "alice"
        capabilities = ["x"]
        backend_id = ""
        status = "idle"
        groups: list = []
        metadata: dict = {}
        registered_at = ""
        last_seen = ""
        def is_alive(self): return True
    p = AgentProfile.build("alice", record=FakeRecord())   # type: ignore[arg-type]
    assert p.is_alive is True


# ─── escape helpers contract ─────────────────────────────────────────


def test_escape_md_handles_pipe():
    assert _escape_md("a|b") == "a\\|b"
    assert _escape_md("") == ""
    assert _escape_md("plain") == "plain"


def test_escape_md_code_neutralises_backticks():
    assert _escape_md_code("a`b") == "a'b"
    assert _escape_md_code("a|b") == "a\\|b"
    assert _escape_md_code("") == ""
