"""AgentProfile — aggregated read-time view of one agent for UI display.

A consolidated snapshot of an agent's identity, capabilities, health,
reputation and contribution data, suitable for showing as a "business
card" in a chat client or web console.

Why this is NOT called ``AgentCard``
------------------------------------
A2A v1.0 reserves the term "Agent Card" for the discovery manifest at
``/.well-known/agent.json``. When the A2A adapter lands (roadmap
v0.11), ``nth_dao.a2a.agent_card`` will be that manifest. To keep that
namespace clean, this UI-side view is called ``AgentProfile``.

Design
------
- **Pure read aggregation** — no persistence, no state. Built on
  demand from the existing subsystem APIs.
- **Composable** — each data source is optional via a typed Protocol.
  Missing sources do not crash; they leave the corresponding fields
  at their dataclass defaults.
- **Display-only** — NOT signed, NOT exchanged across trust
  boundaries. Producing a profile is a local view operation; if you
  need to attest to an agent's reputation, mint an
  ``AchievementCredential`` instead.
- **CJK-safe output** — instead of fixed-width ASCII art (which
  breaks on wide characters), the renderer emits Markdown which the
  UI renders correctly regardless of glyph width.

Design contributed by @andy1868 in the agent-collab submission
(June 2026, original file ``agent_card.py``). This implementation
keeps the dataclass shape and the "compose from optional sources"
idea, drops the bare ``except Exception`` defensiveness in favour of
typed ``Protocol``s, replaces the ASCII-art renderer with a
CJK-safe Markdown renderer, and renames the module to free up the
A2A ``agent_card`` namespace.

Original code: 297 LOC, 7 bare ``except`` blocks, ASCII box-drawing.
This rewrite: ~230 LOC, zero ``except`` blocks, Markdown output.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger("nth_dao.agent_profile")


# ─────────────────────── Source contracts (typed) ───────────────────────


@runtime_checkable
class IdentitySource(Protocol):
    """An ``AgentIdentity``-shaped source."""
    label: str
    pubkey_hex: str

    def as_did(self) -> str: ...


@runtime_checkable
class RecordSource(Protocol):
    """An ``AgentRecord``-shaped source."""
    agent_id: str
    capabilities: List[str]
    backend_id: str
    status: str
    groups: List[str]
    metadata: Dict[str, Any]
    registered_at: str
    last_seen: str

    def is_alive(self) -> bool: ...


@runtime_checkable
class HealthSource(Protocol):
    """A ``FaultIsolator``-shaped source."""

    def agent_health(self, agent_id: str) -> Any: ...


@runtime_checkable
class ReputationSource(Protocol):
    def get_score(self, agent_id: str) -> Any: ...


@runtime_checkable
class LedgerSource(Protocol):
    def stats(self) -> Dict[str, Any]: ...


# ─────────────────────────── Data ────────────────────────────


@dataclass
class AgentProfile:
    """Aggregated read-time view of one agent.

    Every field has a sensible default so a partial build produces a
    well-formed profile; missing data renders as ``"n/a"`` / ``0``.
    """

    agent_id: str
    label: str = ""
    pubkey_fingerprint: str = ""
    did: str = ""

    capabilities: List[str] = field(default_factory=list)
    backend_id: str = ""
    status: str = ""

    groups: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)

    health_score: float = 1.0
    is_alive: bool = True

    reputation_score: float = 0.0
    reputation_count: int = 0

    missions_completed: int = 0
    missions_owned: int = 0
    handoffs_given: int = 0
    handoffs_received: int = 0
    success_rate: float = 0.0

    active_orders: int = 0
    completed_orders: int = 0

    registered_at: str = ""
    last_seen: str = ""

    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── Builder ───────────────────────────────────────────

    @classmethod
    def build(
        cls,
        agent_id: str,
        *,
        identity: Optional[IdentitySource] = None,
        record: Optional[RecordSource] = None,
        health: Optional[HealthSource] = None,
        reputation: Optional[ReputationSource] = None,
        ledger: Optional[LedgerSource] = None,
    ) -> "AgentProfile":
        """Build a profile from whatever sources are available.

        Each source is opt-in. ``None`` leaves the corresponding fields
        at their dataclass defaults. Sources are duck-typed against the
        ``Protocol`` definitions above; passing an object that lacks a
        declared attribute is a *bug in the caller*, not a soft failure
        — it propagates the AttributeError so the test suite catches it.
        """
        profile = cls(agent_id=agent_id)

        if identity is not None:
            profile.label = identity.label
            profile.pubkey_fingerprint = identity.pubkey_hex or ""
            if identity.pubkey_hex:
                # Plain (non-crypto) identities have no DID; check before calling.
                profile.did = identity.as_did()

        if record is not None:
            profile.capabilities = list(record.capabilities or [])
            profile.backend_id = record.backend_id or ""
            profile.status = record.status or ""
            profile.groups = list(record.groups or [])
            meta = record.metadata or {}
            if isinstance(meta, dict):
                profile.roles = list(meta.get("roles", []) or [])
            profile.registered_at = record.registered_at or ""
            profile.last_seen = record.last_seen or ""
            profile.is_alive = bool(record.is_alive())

        if health is not None:
            h = health.agent_health(agent_id)
            score = getattr(h, "health_score", None)
            if isinstance(score, (int, float)):
                profile.health_score = float(score)

        if reputation is not None:
            score = reputation.get_score(agent_id)
            if score is not None:
                # Both a plain number or a dataclass with .score/.count work.
                profile.reputation_score = float(getattr(score, "score", score))
                profile.reputation_count = int(getattr(score, "count", 0))

        if ledger is not None:
            stats = ledger.stats() or {}
            profile.missions_completed = int(stats.get("missions_completed", 0))
            profile.missions_owned = int(stats.get("missions_owned", 0))
            profile.handoffs_given = int(stats.get("handoffs_given", 0))
            profile.handoffs_received = int(stats.get("handoffs_received", 0))
            profile.success_rate = float(stats.get("success_rate", 0.0))

        return profile

    # ── Rendering ─────────────────────────────────────────

    def render_markdown(self) -> str:
        """CJK-safe Markdown rendering.

        Markdown renderers handle character width correctly, so this
        avoids the box-drawing alignment bugs the original ASCII renderer
        exhibited with wide glyphs in ``label`` / ``groups``.
        """
        label = self.label or self.agent_id
        alive_glyph = "🟢 online" if self.is_alive else "⚫ offline"
        bar = _markdown_bar(self.health_score)
        rep_str = (
            f"{self.reputation_score:.1f}/5.0 ({self.reputation_count} ratings)"
            if self.reputation_count else "no ratings yet"
        )
        lines = [
            f"### {label}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Code | `{self.agent_id}` |",
            f"| Status | {alive_glyph} · {self.status or 'n/a'} |",
            f"| Health | {bar} ({self.health_score:.2f}) |",
            f"| DID | `{self.did or 'n/a'}` |",
            f"| Backend | {self.backend_id or 'n/a'} |",
            f"| Capabilities | {', '.join(self.capabilities) or 'n/a'} |",
            f"| Groups | {', '.join(self.groups) or 'n/a'} |",
            f"| Roles | {', '.join(self.roles) or 'n/a'} |",
            f"| Reputation | {rep_str} |",
        ]
        if self.missions_completed or self.missions_owned:
            lines.append(
                f"| Missions | {self.missions_completed} done / "
                f"{self.missions_owned} owned · success {self.success_rate:.0%} |"
            )
        return "\n".join(lines)

    def render_short(self) -> str:
        """One-line summary for log lines or compact lists."""
        label = self.label or self.agent_id
        glyph = "●" if self.is_alive else "○"
        caps = ",".join(self.capabilities[:3]) or "-"
        rep = f"{self.reputation_score:.1f}" if self.reputation_count else "-"
        return f"{glyph} {label}  h={self.health_score:.2f} r={rep}  [{caps}]"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ─────────────────────────── Helpers ────────────────────────────


def _markdown_bar(score: float, width: int = 10) -> str:
    """Render a Markdown-safe health bar: ▰▰▰▰▱▱▱▱▱▱"""
    score = max(0.0, min(1.0, score))
    filled = int(score * width)
    return "▰" * filled + "▱" * (width - filled)


__all__ = [
    "AgentProfile",
    "IdentitySource",
    "RecordSource",
    "HealthSource",
    "ReputationSource",
    "LedgerSource",
]
