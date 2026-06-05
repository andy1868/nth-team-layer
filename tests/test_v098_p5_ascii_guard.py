"""P5: protocol-layer source must stay ASCII-only.

Project history has multiple Windows / GBK / mojibake incidents
(see PR #7, the original event_bus.py linter corruption, the
lan_mdns.py garbling). Stick to ASCII in protocol-layer files so a
mis-configured environment can't silently corrupt them.

Display literals (block chars, online/offline glyphs) are still
permitted via an explicit allow-list at the top of this file: those
are EXPLICIT product output, not incidental decoration.
"""

from __future__ import annotations

import pathlib

import pytest


# Allow-listed codepoints: display literals that this layer DOES use
# intentionally. Each entry must come with the file(s) it's allowed in
# and a one-line justification - if a new codepoint shows up that isn't
# on this list, the test fails and forces the contributor to declare it.
DISPLAY_ALLOWED = {
    0x02CB,   # MODIFIER LETTER GRAVE ACCENT (agent_profile escape replacement)
    0x25B0,   # BLACK SQUARE (health bar filled glyph)
    0x25B1,   # WHITE SQUARE (health bar empty glyph)
    0x25CB,   # WHITE CIRCLE (offline glyph for short render)
    0x25CF,   # BLACK CIRCLE (online glyph for short render)
    0x26AB,   # MEDIUM BLACK CIRCLE (markdown render offline)
    0x1F7E2,  # LARGE GREEN CIRCLE (markdown render online)
}

PROTOCOL_LAYER_FILES = [
    "nth_dao/action_routing.py",
    "nth_dao/event_bus.py",
    "nth_dao/event_subscriptions.py",
    "nth_dao/fault_isolation.py",
    "nth_dao/agent_profile.py",
]


@pytest.mark.parametrize("path", PROTOCOL_LAYER_FILES)
def test_P5_protocol_layer_is_ASCII(path: str):
    """Every byte in these files is either ASCII or on the small
    DISPLAY_ALLOWED list. New non-ASCII characters in protocol code
    must be either ASCII-ified or added to DISPLAY_ALLOWED with a
    one-line justification."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    offenders = sorted({
        c for c in text
        if ord(c) > 127 and ord(c) not in DISPLAY_ALLOWED
    })
    assert not offenders, (
        f"{path}: non-ASCII chars not on the allow-list: "
        + ", ".join(f"U+{ord(c):04X}" for c in offenders)
        + "\nEither ASCII-ify them or add to DISPLAY_ALLOWED with a "
          "justification comment."
    )
