"""Architect R-9 + R-14 (2026-06-07): trim is now iterative.

R-9  - The H-1 truncation guard only peeled ONE partial \\uXXXX escape
       per call. A truncation landing inside ``foo\\\\u003c\\u`` would
       leave a stray trailing ``\\`` after one peel. We now iterate
       until a stable boundary is reached.

R-14 - The H-1 fuzz only tested bodies of repeated ``<``, exercising a
       single escape form. Real prompt bodies also contain literal
       backslashes (Python source pastes, regex examples, etc.). This
       expands the fuzz to a mix.
"""

from __future__ import annotations

from dataclasses import dataclass

from nth_dao.agent_daemon import AgentDaemon, DaemonConfig


@dataclass
class _Msg:
    sender_id: str
    body: str


class _FakeTeam:
    agent_id = "agent-r9-test"
    workspace = "."


def _daemon(max_context_chars=4000, max_context_messages=10):
    cfg = DaemonConfig(
        channel_ids=["general"],
        max_context_messages=max_context_messages,
        max_context_chars=max_context_chars,
    )
    return AgentDaemon(team=_FakeTeam(), config=cfg)


# ===== R-9 directly on the trimmer =====


def test_R9_trim_one_call_handles_consecutive_partial_escapes():
    """Construct a string with two adjacent partial escapes at the end.
    A single peel would leave residue; the iterative version peels
    until stable."""
    # ``foo\u003`` would normally peel to ``foo``. But what about
    # ``foo\\\\u``? After one peel the trailing ``\\u`` is dropped,
    # leaving ``foo\\\\``. Without iteration that stray backslash
    # remains and creates downstream parse ambiguity.
    s = "foo\\\\u"
    out = AgentDaemon._trim_dangling_unicode_escape(s)
    # The double backslash IS a complete sequence in source-pasted body
    # (literal "\\"); we conservatively prefer dropping ambiguity.
    # Either way the result must NOT end with a stray "\\".
    assert not out.endswith("\\"), (
        f"iterative trim left stray backslash: {out!r}"
    )


def test_R9_trim_is_idempotent():
    """Calling twice gives the same result as once - stable fixpoint."""
    s = "msg\\u003c\\u00"
    once = AgentDaemon._trim_dangling_unicode_escape(s)
    twice = AgentDaemon._trim_dangling_unicode_escape(once)
    assert once == twice


def test_R9_trim_preserves_complete_escapes():
    """Complete \\uXXXX sequences must not be touched - we only peel
    partials at the tail."""
    s = "shipped on \\u003c2026-01-01\\u003e"
    out = AgentDaemon._trim_dangling_unicode_escape(s)
    assert out == s


def test_R9_trim_preserves_clean_text():
    """No backslash anywhere - return unchanged."""
    s = "plain ascii text with no escapes"
    assert AgentDaemon._trim_dangling_unicode_escape(s) == s


# ===== R-14 fuzz with mixed escape patterns =====


def test_R14_fuzz_bodies_with_literal_backslashes_never_leave_dangling():
    """Mixed body fuzz: literal backslashes + escape-triggering chars
    + non-ASCII at various positions. Output must never end with a
    partial unicode escape regardless of where truncation lands."""
    test_bodies = [
        # Literal Python-source snippet
        "path = \"C:\\\\Users\\\\foo\" and <tag>",
        # Backslash next to escape trigger
        "\\\\ + <html>",
        # Many alternating types
        ("\\<\\>\\&" * 20),
        # The H-1 fuzz pattern
        "<" * 100,
        # Mixed ampersand & angle
        "&<>" * 60,
        # Trailing literal backslash
        "msg with trailing \\",
    ]
    for body in test_bodies:
        for budget in range(8, 200, 11):
            daemon = _daemon(max_context_chars=budget)
            out = daemon._build_untrusted_context([_Msg("u", body)])
            for partial in ("\\u", "\\u0", "\\u00", "\\u003"):
                assert not out.endswith(partial), (
                    f"body={body[:20]!r} budget={budget}: ends with "
                    f"{partial!r}; tail={out[-10:]!r}"
                )


def test_R14_fuzz_long_body_with_consecutive_special_chars():
    """A body of solid <>&<>&... never produces a dangling escape at
    any truncation boundary."""
    body = "<>&" * 200
    for budget in range(6, 400, 7):
        daemon = _daemon(max_context_chars=budget)
        out = daemon._build_untrusted_context([_Msg("u", body)])
        # Never ends with a stray backslash either - R-9 strictness
        assert not out.endswith("\\"), (
            f"budget={budget}: stray backslash at boundary; "
            f"tail={out[-10:]!r}"
        )
