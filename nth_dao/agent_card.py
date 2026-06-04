"""
Agent Card — aggregated agent profile for discovery and display.

An AgentCard pulls together identity, capabilities, health, reputation,
and contribution data into a single snapshot.  It's the "business card"
that other agents see when they discover a peer.

Usage::

    card = AgentCard.build(
        agent_id="alice",
        identity=team.identity,
        registry=team.registry,
    )
    print(card.render())     # ASCII table for terminal
    print(card.to_json())    # JSON for API/web

Integration::

    import nth_dao as nth
    team = nth.attach(agent_id="alice", ...)
    card = team.card("alice")    # via TeamSession.card()

Design
------

- Zero external dependencies — pure stdlib.
- Composable: each data source is optional; missing sources produce
  ``"n/a"`` instead of crashing.
- Renders to ASCII table and JSON dict.
- Stateless: no persistence — it's a read-time aggregation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util.io import safe_load_json

logger = logging.getLogger("nth_dao.agent_card")


# ────────────────────────── Data types ──────────────────────────


@dataclass
class AgentCard:
    """Aggregated agent profile — the "business card" for discovery."""

    agent_id: str
    label: str = ""                      # human-readable name
    pubkey_fingerprint: str = ""         # Ed25519 pubkey fingerprint (hex)

    # Identity
    did: str = ""                        # W3C did:key

    # Capabilities (from agent_registry)
    capabilities: List[str] = field(default_factory=list)
    backend_id: str = ""
    status: str = ""                     # idle / busy / offline

    # Groups & roles
    groups: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)

    # Health (from fault_isolation or AgentRegistry)
    health_score: float = 1.0
    is_alive: bool = True

    # Reputation (from reputation.py)
    reputation_score: float = 0.0        # 0.0–5.0
    reputation_count: int = 0

    # Contributions (from agent_ledger.py)
    missions_completed: int = 0
    missions_owned: int = 0
    handoffs_given: int = 0
    handoffs_received: int = 0
    success_rate: float = 0.0

    # Marketplace
    active_orders: int = 0
    completed_orders: int = 0

    # Timestamps
    registered_at: str = ""
    last_seen: str = ""

    # Metadata catch-all
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── Builder ───────────────────────────────────────────

    @classmethod
    def build(
        cls,
        agent_id: str,
        *,
        identity: Any = None,
        registry: Any = None,
        reputation_manager: Any = None,
        ledger: Any = None,
        marketplace: Any = None,
        fault_isolator: Any = None,
    ) -> "AgentCard":
        """Build an AgentCard from available data sources.

        Each source is optional — missing sources produce defaults.
        """
        card = cls(agent_id=agent_id)

        # Identity
        if identity is not None:
            try:
                card.label = getattr(identity, "label", "")
                card.pubkey_fingerprint = getattr(identity, "pubkey_hex", "") or ""
                if hasattr(identity, "as_did"):
                    card.did = identity.as_did()
            except Exception:
                logger.debug("failed to extract identity fields", exc_info=True)

        # Registry (AgentRecord)
        if registry is not None:
            try:
                record = _get_agent_record(registry, agent_id)
                if record:
                    card.capabilities = getattr(record, "capabilities", []) or []
                    card.backend_id = getattr(record, "backend_id", "") or ""
                    card.status = getattr(record, "status", "") or ""

                    groups = getattr(record, "groups", []) or []
                    card.groups = list(groups)

                    meta = getattr(record, "metadata", {}) or {}
                    if isinstance(meta, dict):
                        card.roles = meta.get("roles", [])

                    card.registered_at = getattr(record, "registered_at", "") or ""
                    card.last_seen = getattr(record, "last_seen", "") or ""
                    card.is_alive = getattr(record, "is_alive", lambda: True)()
            except Exception:
                logger.debug("failed to extract registry fields for %r", agent_id, exc_info=True)

        # Fault Isolator
        if fault_isolator is not None:
            try:
                h = fault_isolator.agent_health(agent_id)
                card.health_score = h.health_score
            except Exception:
                logger.debug("failed to extract health for %r", agent_id, exc_info=True)

        # Reputation
        if reputation_manager is not None:
            try:
                score = reputation_manager.get_score(agent_id)
                if score:
                    card.reputation_score = score.score if hasattr(score, "score") else score
                    card.reputation_count = score.count if hasattr(score, "count") else 0
            except Exception:
                logger.debug("failed to extract reputation for %r", agent_id, exc_info=True)

        # Ledger
        if ledger is not None:
            try:
                stats = ledger.stats() if callable(getattr(ledger, "stats", None)) else {}
                card.missions_completed = stats.get("missions_completed", 0)
                card.missions_owned = stats.get("missions_owned", 0)
                card.handoffs_given = stats.get("handoffs_given", 0)
                card.handoffs_received = stats.get("handoffs_received", 0)
                card.success_rate = stats.get("success_rate", 0.0)
            except Exception:
                logger.debug("failed to extract ledger stats for %r", agent_id, exc_info=True)

        # Marketplace
        if marketplace is not None:
            try:
                card.active_orders = len(getattr(marketplace, "list_active", lambda: [])())
                card.completed_orders = len(getattr(marketplace, "list_completed", lambda: [])())
            except Exception:
                logger.debug("failed to extract marketplace stats for %r", agent_id, exc_info=True)

        return card

    # ── Rendering ─────────────────────────────────────────

    def render(self) -> str:
        """Return an ASCII table representation for terminal display."""
        label = self.label or self.agent_id
        alive = "● online" if self.is_alive else "○ offline"
        health_bar = _health_bar(self.health_score)

        lines = [
            f"╔══════════════════════════════════════════════════╗",
            f"║  {label:46s}  ║",
            f"╠══════════════════════════════════════════════════╣",
            f"║  DID      │ {_trunc(self.did, 36) if self.did else 'n/a':36s} ║",
            f"║  Pubkey   │ {_trunc(self.pubkey_fingerprint, 36):36s} ║",
            f"╠══════════════════════════════════════════════════╣",
            f"║  Status   │ {self.status or 'n/a':12s}  {alive:10s}  {health_bar:16s} ║",
            f"║  Backend  │ {self.backend_id or 'n/a':36s} ║",
            f"╠══════════════════════════════════════════════════╣",
            f"║  Caps     │ {', '.join(self.capabilities[:5]) or 'n/a':36s} ║",
            f"║  Groups   │ {', '.join(self.groups[:5]) or 'n/a':36s} ║",
            f"║  Roles    │ {', '.join(self.roles[:3]) or 'n/a':36s} ║",
            f"╠══════════════════════════════════════════════════╣",
            f"║  Health   │ {self.health_score:.2f}  Rep: {self.reputation_score:.1f}/5.0 ({self.reputation_count} ratings) {'':>8s} ║",
        ]

        if self.missions_completed or self.missions_owned:
            lines.append(
                f"║  Missions │ {self.missions_completed} done / {self.missions_owned} owned  "
                f"SR: {self.success_rate:.0%} {'':>13s} ║"
            )

        lines.append(
            f"╚══════════════════════════════════════════════════╝"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "pubkey_fingerprint": self.pubkey_fingerprint,
            "did": self.did,
            "capabilities": self.capabilities,
            "backend_id": self.backend_id,
            "status": self.status,
            "groups": self.groups,
            "roles": self.roles,
            "health_score": self.health_score,
            "is_alive": self.is_alive,
            "reputation_score": self.reputation_score,
            "reputation_count": self.reputation_count,
            "missions_completed": self.missions_completed,
            "missions_owned": self.missions_owned,
            "handoffs_given": self.handoffs_given,
            "handoffs_received": self.handoffs_received,
            "success_rate": self.success_rate,
            "active_orders": self.active_orders,
            "completed_orders": self.completed_orders,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        """Return a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def short(self) -> str:
        """One-line summary."""
        label = self.label or self.agent_id
        caps = ",".join(self.capabilities[:3]) or "-"
        rep = f"{self.reputation_score:.1f}" if self.reputation_count else "-"
        return (
            f"{'●' if self.is_alive else '○'} {label:20s} "
            f"h={self.health_score:.2f} r={rep} "
            f"[{caps}]"
        )


# ────────────────────────── Helpers ──────────────────────────


def _get_agent_record(registry: Any, agent_id: str) -> Any:
    """Extract an AgentRecord from a registry, handling various shapes."""
    # Try registry.get_record() or iterate list_all()
    if hasattr(registry, "get_record"):
        return registry.get_record(agent_id)
    if hasattr(registry, "list_all"):
        for r in registry.list_all():
            if getattr(r, "agent_id", "") == agent_id:
                return r
    if hasattr(registry, "list_alive"):
        for r in registry.list_alive():
            if getattr(r, "agent_id", "") == agent_id:
                return r
    return None


def _health_bar(score: float, width: int = 10) -> str:
    """Render a simple health bar: ████░░░░░░"""
    filled = max(0, min(width, int(score * width)))
    return "█" * filled + "░" * (width - filled)


def _trunc(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len - 1] + "…"
