"""Tests for nth_dao.agent_profile — aggregated read-time agent view.

Renamed from andy1868's original ``agent_card.py`` (which collided with
A2A's ``Agent Card`` namespace reserved for v0.11). This rewrite drops
the bare ``except Exception`` defensiveness in favour of typed
Protocols and replaces the ASCII box-drawing renderer with CJK-safe
Markdown output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

from nth_dao.agent_profile import AgentProfile


# ─── source doubles ────────────────────────────────────────────────────


@dataclass
class FakeIdentity:
    label: str = ""
    pubkey_hex: str = ""

    def as_did(self) -> str:
        return f"did:key:z{self.pubkey_hex[:16]}"


@dataclass
class FakeRecord:
    agent_id: str = ""
    capabilities: List[str] = field(default_factory=list)
    backend_id: str = ""
    status: str = ""
    groups: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    registered_at: str = ""
    last_seen: str = ""
    alive: bool = True

    def is_alive(self) -> bool:
        return self.alive


@dataclass
class FakeHealthValue:
    health_score: float


class FakeHealth:
    def __init__(self, score: float = 1.0):
        self.score = score

    def agent_health(self, _agent_id: str):
        return FakeHealthValue(health_score=self.score)


@dataclass
class FakeRepScore:
    score: float
    count: int


class FakeReputation:
    def __init__(self, score: float, count: int):
        self._score = FakeRepScore(score=score, count=count)

    def get_score(self, _agent_id: str):
        return self._score


class FakeLedger:
    def __init__(self, **stats):
        self._stats = stats

    def stats(self):
        return dict(self._stats)


# ─── tests ─────────────────────────────────────────────────────────────


def test_defaults():
    p = AgentProfile(agent_id="alice")
    assert p.agent_id == "alice"
    assert p.health_score == 1.0
    # M-4 fix: unknown agents (no record source) default to OFFLINE,
    # not online. The previous default of True silently asserted
    # liveness it had no data to support.
    assert p.is_alive is False
    assert p.reputation_count == 0


def test_build_with_no_sources_keeps_defaults():
    p = AgentProfile.build("solo")
    assert p.agent_id == "solo"
    assert p.label == ""
    assert p.capabilities == []


def test_build_with_identity():
    ident = FakeIdentity(label="Alice", pubkey_hex="a" * 64)
    p = AgentProfile.build("alice", identity=ident)
    assert p.label == "Alice"
    assert p.pubkey_fingerprint == "a" * 64
    assert p.did.startswith("did:key:z")


def test_build_with_record():
    rec = FakeRecord(
        agent_id="bob",
        capabilities=["python", "web"],
        backend_id="claude-code",
        status="idle",
        groups=["bots", "ops"],
        metadata={"roles": ["reviewer"]},
        registered_at="2026-06-01T00:00:00",
        last_seen="2026-06-02T01:00:00",
        alive=False,
    )
    p = AgentProfile.build("bob", record=rec)
    assert p.capabilities == ["python", "web"]
    assert p.backend_id == "claude-code"
    assert p.status == "idle"
    assert p.groups == ["bots", "ops"]
    assert p.roles == ["reviewer"]
    assert p.is_alive is False
    assert p.last_seen == "2026-06-02T01:00:00"


def test_build_with_health():
    p = AgentProfile.build("alice", health=FakeHealth(score=0.42))
    assert p.health_score == pytest.approx(0.42)


def test_build_with_reputation():
    p = AgentProfile.build("alice", reputation=FakeReputation(score=4.3, count=12))
    assert p.reputation_score == pytest.approx(4.3)
    assert p.reputation_count == 12


def test_build_with_ledger():
    p = AgentProfile.build("alice", ledger=FakeLedger(
        missions_completed=5,
        missions_owned=2,
        handoffs_given=3,
        handoffs_received=4,
        success_rate=0.85,
    ))
    assert p.missions_completed == 5
    assert p.missions_owned == 2
    assert p.handoffs_given == 3
    assert p.handoffs_received == 4
    assert p.success_rate == pytest.approx(0.85)


def test_build_with_all_sources_composes_full_profile():
    p = AgentProfile.build(
        "alice",
        identity=FakeIdentity(label="Alice", pubkey_hex="d" * 64),
        record=FakeRecord(agent_id="alice", capabilities=["x"], status="busy"),
        health=FakeHealth(score=0.6),
        reputation=FakeReputation(score=4.0, count=10),
        ledger=FakeLedger(missions_completed=7, success_rate=0.9),
    )
    assert p.label == "Alice"
    assert p.status == "busy"
    assert p.health_score == pytest.approx(0.6)
    assert p.reputation_score == pytest.approx(4.0)
    assert p.missions_completed == 7


# ─── rendering ─────────────────────────────────────────────────────────


def test_render_markdown_contains_key_fields():
    p = AgentProfile.build(
        "alice",
        identity=FakeIdentity(label="Alice", pubkey_hex="b" * 64),
        record=FakeRecord(agent_id="alice", capabilities=["python"], groups=["bots"]),
    )
    md = p.render_markdown()
    assert "### Alice" in md
    assert "| Code | `alice` |" in md
    assert "python" in md
    assert "bots" in md
    # Markdown table header is present
    assert "| Field | Value |" in md


def test_render_markdown_handles_cjk_labels():
    """The ASCII renderer broke on wide glyphs. Markdown handles them."""
    p = AgentProfile.build(
        "alice",
        identity=FakeIdentity(label="爱丽丝的工作站", pubkey_hex="c" * 64),
        record=FakeRecord(agent_id="alice", groups=["技术组"]),
    )
    md = p.render_markdown()
    assert "爱丽丝的工作站" in md
    assert "技术组" in md
    # No misalignment to worry about; Markdown is render-time CJK-correct.


def test_render_markdown_health_bar_bounded():
    """Health bar must handle scores outside [0,1] gracefully."""
    p = AgentProfile(agent_id="a", health_score=2.5)   # over 1.0
    md = p.render_markdown()
    assert "#" * 10 in md     # clamped to full bar

    p = AgentProfile(agent_id="b", health_score=-1.0)  # under 0.0
    md = p.render_markdown()
    assert "-" * 10 in md     # clamped to empty bar


def test_render_short_one_line():
    p = AgentProfile(
        agent_id="alice",
        label="Alice",
        is_alive=True,
        health_score=0.85,
        reputation_score=4.2,
        reputation_count=3,
        capabilities=["a", "b", "c", "d"],
    )
    s = p.render_short()
    assert s.startswith("*")
    assert "Alice" in s
    assert "h=0.85" in s
    # Only first 3 caps shown
    assert "a,b,c" in s
    assert "d" not in s.split("[", 1)[1]


def test_to_dict_round_trip():
    p = AgentProfile(agent_id="alice", capabilities=["x", "y"])
    d = p.to_dict()
    assert d["agent_id"] == "alice"
    assert d["capabilities"] == ["x", "y"]


def test_to_json_valid_and_unicode_safe():
    p = AgentProfile(agent_id="alice", label="爱丽丝")
    j = p.to_json()
    parsed = json.loads(j)
    assert parsed["label"] == "爱丽丝"


# ─── Protocol contract enforcement (no silent failures) ───────────────


def test_passing_object_missing_required_attribute_raises():
    """A caller passing a broken source should see the AttributeError —
    NOT have it swallowed by a bare except. That's the whole point of
    typed Protocols vs the original bare-except defensiveness."""
    class BrokenIdentity:
        pubkey_hex = "x" * 64
        # missing .label and .as_did()
    with pytest.raises(AttributeError):
        AgentProfile.build("alice", identity=BrokenIdentity())   # type: ignore[arg-type]


def test_identity_without_pubkey_skips_did_lookup():
    """A plain (non-crypto) identity with empty pubkey shouldn't try
    to resolve a DID."""
    class PlainIdentity:
        label = "anon"
        pubkey_hex = ""

        def as_did(self) -> str:
            raise RuntimeError("would have been called incorrectly")
    p = AgentProfile.build("anon", identity=PlainIdentity())   # type: ignore[arg-type]
    assert p.label == "anon"
    assert p.did == ""
