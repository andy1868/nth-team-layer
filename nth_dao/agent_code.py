"""Stable, human-readable agent identifiers ("agent codes").

Background
----------
Telegram-style or QQ-style apps show a short, fixed, *visible* code per
contact (Telegram username, QQ number) so humans can search and add
each other without having to copy long pubkeys around. NTH DAO already
has `AgentID` (short, but only present for crypto-derived agents) and
`pubkey_hex` (long, 64 hex). Neither is ideal as a copy-and-paste handle
in a chat UI.

This module defines one canonical short code per agent::

    pubkey_hex  → SHA-256 → first 8 hex → "a3f7b2e8" → format "a3f7-b2e8"

The 8-hex format gives 4.3 billion code-space; with displayed dash it
becomes a typeable, screen-readable handle that survives a phone call.
Collisions are detected at lookup time (the registry stores the full
pubkey too) so any two agents whose code accidentally collides can
still be disambiguated by their `pubkey_hex`.

For agents that have no pubkey (the "anonymous home admin" case), the
fallback derives the code from `sha256(agent_id_string)` so the code
is still stable and unique per `agent_id`.

API
---
    >>> from nth_dao.agent_code import code_for_pubkey, code_for_agent_id, parse_code
    >>> code_for_pubkey("a3" * 32)
    'd4a4-a8f7'
    >>> code_for_agent_id("alice")
    '2bd8-04ee'
    >>> parse_code("d4a4-a8f7")
    'd4a4a8f7'
    >>> parse_code("d4a4a8f7")
    'd4a4a8f7'

Used by:
- the web console (member badges, conversation senders, actor header)
- the search endpoint (`/api/agents/search?code=...`)
- the demo responder (matches incoming messages by sender code)
"""

from __future__ import annotations

import hashlib
import re

CODE_LEN = 8           # hex chars
DISPLAY_GROUP = 4      # dash every N chars
_CODE_RE = re.compile(r"^[0-9a-f-]+$")


def code_for_pubkey(pubkey_hex: str) -> str:
    """Return the formatted code derived from an Ed25519 pubkey hex.

    Stable: same pubkey → same code, forever. Empty string in → empty out
    so callers don't need to gate.
    """
    if not pubkey_hex:
        return ""
    digest = hashlib.sha256(pubkey_hex.encode("utf-8")).hexdigest()
    return _format(digest[:CODE_LEN])


def code_for_agent_id(agent_id: str) -> str:
    """Fallback code for plain (non-crypto) agent_ids.

    The home workspace's bootstrap admin and any agent_id-only members
    take this path; the result is still stable per agent_id string.
    """
    if not agent_id:
        return ""
    digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
    return _format(digest[:CODE_LEN])


def parse_code(value: str) -> str:
    """Normalise a user-typed code to its dash-less hex form.

    Accepts ``"a3f7-b2e8"``, ``"a3f7b2e8"``, ``"A3F7-B2E8"`` — returns
    ``"a3f7b2e8"``. Raises ValueError on anything that isn't a valid
    8-hex code.
    """
    if not isinstance(value, str):
        raise ValueError("code must be a string")
    raw = value.strip().lower().replace("-", "")
    if len(raw) != CODE_LEN or not all(c in "0123456789abcdef" for c in raw):
        raise ValueError(f"invalid agent code {value!r}; expected 8 hex chars")
    return raw


def _format(hex_str: str) -> str:
    """``"d4a4a8f7"`` → ``"d4a4-a8f7"`` (dash every 4 chars)."""
    return "-".join(
        hex_str[i:i + DISPLAY_GROUP] for i in range(0, len(hex_str), DISPLAY_GROUP)
    )


__all__ = [
    "CODE_LEN",
    "code_for_pubkey",
    "code_for_agent_id",
    "parse_code",
]
