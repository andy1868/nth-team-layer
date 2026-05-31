"""Web-of-Trust — endorsement-based multi-hop trust propagation.

Problem this solves:
    The fixed `trust_anchors` map in GossipNode only lets you accept gossip
    from agents whose pubkey you've manually pinned. In a decentralized A2A
    network, you want to bootstrap trust by *transitive vouching*:

        Alice trusts Bob.
        Bob trusts Carol.
        Therefore Alice can accept Carol's messages — at depth 2, expiring,
        and only if Alice has explicitly enabled transitive trust.

Design:
    1. An endorsement is a signed JSON object:
        {
            "endorser_pubkey": "<hex>",
            "subject_pubkey":  "<hex>",
            "subject_agent_id": "carol",
            "depth_allowed":   2,    # how many further hops this endorsement covers
            "context":         "general",  # optional scope (e.g., "code_review")
            "issued_at":       "<iso>",
            "expires_at":      "<iso>",
            "sig":             "<hex>"   # endorser signs canonical of all above
        }

    2. A TrustGraph stores endorsements in `team_trust/endorsements.jsonl` and
       maintains a derived `agent_id → pubkey` resolution that lets a caller
       check `is_trusted(agent_id, pubkey, *, max_depth=2)`.

    3. Verification is cheap: BFS from the local root-trusted pubkeys; at each
       hop verify the endorsement signature against the previous-hop pubkey.
       The walk is bounded by `max_depth`.

This module is **stdlib + nth_dao.identity only** — no third-party crypto
beyond optional PyNaCl (already provided by `[crypto]` extra).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from .identity import (
    AgentIdentity,
    _NACL_AVAILABLE,
    _VerifyKey,
    canonical_json,
)
from .util import atomic_write_json, safe_load_json, safe_id

logger = logging.getLogger("nth_dao.web_of_trust")


DEFAULT_TRUST_DIR = "team_trust"
DEFAULT_ENDORSEMENT_TTL_DAYS = 90
DEFAULT_MAX_DEPTH = 2  # conservative default — 2 hops max
MAX_PROPAGATION_DEPTH = 5  # absolute cap regardless of caller request


@dataclass
class Endorsement:
    """One signed vouch: endorser declares subject's pubkey is legitimate."""

    endorser_pubkey: str
    subject_pubkey: str
    subject_agent_id: str
    depth_allowed: int = 1
    context: str = "general"
    issued_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expires_at: str = ""
    sig: str = ""

    def signable_dict(self) -> dict:
        d = asdict(self)
        d.pop("sig", None)
        return d

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Endorsement":
        return cls(
            endorser_pubkey=data.get("endorser_pubkey", ""),
            subject_pubkey=data.get("subject_pubkey", ""),
            subject_agent_id=data.get("subject_agent_id", ""),
            depth_allowed=int(data.get("depth_allowed", 1)),
            context=data.get("context", "general"),
            issued_at=data.get("issued_at", ""),
            expires_at=data.get("expires_at", ""),
            sig=data.get("sig", ""),
        )

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.fromisoformat(self.expires_at) < datetime.now()
        except ValueError:
            return True

    def verify_sig(self) -> bool:
        """Verify endorser_pubkey signed this endorsement."""
        if not (_NACL_AVAILABLE and _VerifyKey and self.sig and self.endorser_pubkey):
            return False
        try:
            payload = canonical_json(self.signable_dict())
            _VerifyKey(bytes.fromhex(self.endorser_pubkey)).verify(
                payload, bytes.fromhex(self.sig),
            )
            return True
        except Exception:
            return False


def issue_endorsement(
    endorser: AgentIdentity,
    subject_pubkey: str,
    subject_agent_id: str,
    depth_allowed: int = 1,
    context: str = "general",
    ttl_days: int = DEFAULT_ENDORSEMENT_TTL_DAYS,
) -> Endorsement:
    """Mint and sign an endorsement.

    The endorser must hold a crypto-capable AgentIdentity (PyNaCl).
    """
    if not endorser.can_sign:
        raise ValueError(
            "issue_endorsement requires endorser to have a signing key "
            "(AgentIdentity.generate with pynacl)"
        )
    if not subject_pubkey:
        raise ValueError("subject_pubkey required")
    if depth_allowed < 1 or depth_allowed > MAX_PROPAGATION_DEPTH:
        raise ValueError(
            f"depth_allowed must be in [1, {MAX_PROPAGATION_DEPTH}]"
        )
    expires_at = (datetime.now() + timedelta(days=ttl_days)).isoformat()
    e = Endorsement(
        endorser_pubkey=endorser.pubkey_hex,
        subject_pubkey=subject_pubkey,
        subject_agent_id=subject_agent_id,
        depth_allowed=depth_allowed,
        context=context,
        issued_at=datetime.now().isoformat(),
        expires_at=expires_at,
    )
    e.sig = endorser.sign_json(e.signable_dict())
    return e


class TrustGraph:
    """Append-only endorsement store + BFS-based trust resolution.

    Storage layout:
        team_trust/
            endorsements.jsonl    # all known endorsements, one per line
            roots.json            # locally-pinned root pubkeys + their agent_ids

    Roots are the seed trust anchors a caller manually pinned (typically
    the agent's own pubkey + a small set of bootstrap peers). Transitive
    trust extends from these.
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        trust_dir: str = DEFAULT_TRUST_DIR,
    ):
        self.workspace = Path(workspace)
        self.base_dir = self.workspace / trust_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._endorsements_path = self.base_dir / "endorsements.jsonl"
        self._roots_path = self.base_dir / "roots.json"

    # ─── roots ─────────────────────────────────────────────────────────

    def add_root(self, agent_id: str, pubkey_hex: str) -> None:
        """Pin a pubkey as a root of trust (no signature required)."""
        if not pubkey_hex or not agent_id:
            raise ValueError("agent_id and pubkey_hex required")
        roots = self._load_roots()
        existing = roots.get(agent_id)
        if existing and existing != pubkey_hex:
            logger.warning(
                "TrustGraph.add_root: rotating root pubkey for %s "
                "(was %s..)", agent_id, existing[:16],
            )
        roots[agent_id] = pubkey_hex
        atomic_write_json(self._roots_path, roots)

    def remove_root(self, agent_id: str) -> bool:
        roots = self._load_roots()
        if agent_id in roots:
            del roots[agent_id]
            atomic_write_json(self._roots_path, roots)
            return True
        return False

    def roots(self) -> Dict[str, str]:
        """Return {agent_id: pubkey_hex} of root-trusted agents."""
        return dict(self._load_roots())

    def _load_roots(self) -> Dict[str, str]:
        data = safe_load_json(self._roots_path, fallback={})
        return data if isinstance(data, dict) else {}

    # ─── endorsements ──────────────────────────────────────────────────

    def import_endorsement(self, e: Endorsement) -> bool:
        """Validate and store an endorsement. Returns True if accepted."""
        if not e.endorser_pubkey or not e.subject_pubkey:
            return False
        if e.is_expired:
            logger.debug("rejected expired endorsement of %s", e.subject_agent_id)
            return False
        if not e.verify_sig():
            logger.warning(
                "rejected endorsement of %s — signature does not verify against endorser",
                e.subject_agent_id,
            )
            return False
        if self._already_known(e):
            return False
        self._append(e)
        return True

    def list_endorsements(
        self,
        endorser_pubkey: Optional[str] = None,
        subject_pubkey: Optional[str] = None,
        include_expired: bool = False,
    ) -> List[Endorsement]:
        out = []
        for e in self._load_all():
            if endorser_pubkey and e.endorser_pubkey != endorser_pubkey:
                continue
            if subject_pubkey and e.subject_pubkey != subject_pubkey:
                continue
            if not include_expired and e.is_expired:
                continue
            out.append(e)
        return out

    def _already_known(self, e: Endorsement) -> bool:
        for existing in self._load_all():
            if (
                existing.endorser_pubkey == e.endorser_pubkey
                and existing.subject_pubkey == e.subject_pubkey
                and existing.issued_at == e.issued_at
            ):
                return True
        return False

    def _load_all(self) -> List[Endorsement]:
        if not self._endorsements_path.exists():
            return []
        out = []
        try:
            lines = self._endorsements_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Endorsement.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return out

    def _append(self, e: Endorsement) -> None:
        self._endorsements_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._endorsements_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    # ─── trust resolution ──────────────────────────────────────────────

    def is_trusted(
        self,
        agent_id: str,
        pubkey_hex: str,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        context: Optional[str] = None,
    ) -> bool:
        """True iff there's a valid endorsement chain from a root to (agent_id, pubkey).

        Roots count as depth 0. A direct endorsement from a root = depth 1.
        max_depth caps how far we'll walk (also bounded by MAX_PROPAGATION_DEPTH
        and the issuing endorser's `depth_allowed`).
        """
        path = self.resolve_path(
            agent_id, pubkey_hex,
            max_depth=max_depth, context=context,
        )
        return path is not None

    def resolve_path(
        self,
        agent_id: str,
        pubkey_hex: str,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        context: Optional[str] = None,
    ) -> Optional[List[str]]:
        """BFS the endorsement graph; return list of pubkeys forming the trust chain,
        or None if no chain within max_depth.

        Root → ... → subject. Returns ["<root_pubkey>", ..., "<subject_pubkey>"].
        """
        roots = self._load_roots()
        if not pubkey_hex:
            return None
        # Direct root hit (depth 0)
        if pubkey_hex in roots.values():
            # Verify agent_id matches the root mapping
            for aid, pk in roots.items():
                if pk == pubkey_hex and aid == agent_id:
                    return [pubkey_hex]
            # pubkey matched but agent_id didn't — that's a name spoof
            return None

        bounded_depth = min(max_depth, MAX_PROPAGATION_DEPTH)
        endorsements = [e for e in self._load_all() if not e.is_expired]
        if context:
            endorsements = [
                e for e in endorsements
                if e.context in ("general", context)
            ]

        # Index by endorser_pubkey for quick BFS expansion
        by_endorser: Dict[str, List[Endorsement]] = {}
        for e in endorsements:
            by_endorser.setdefault(e.endorser_pubkey, []).append(e)

        # BFS from each root pubkey.
        # Queue items: (current_pubkey, depth, allowed_further, path_so_far)
        # allowed_further = how many MORE hops we can take starting from cur_pk.
        # Roots have allowed_further = bounded_depth (infinite-ish within budget).
        queue: deque = deque()
        seen: Set[str] = set()
        for root_pk in roots.values():
            queue.append((root_pk, 0, bounded_depth, [root_pk]))
            seen.add(root_pk)

        while queue:
            cur_pk, depth, allowed_further, path = queue.popleft()
            if depth >= bounded_depth or allowed_further <= 0:
                continue
            for e in by_endorser.get(cur_pk, []):
                # Did this hop reach the target?
                if e.subject_pubkey == pubkey_hex and e.subject_agent_id == agent_id:
                    return path + [e.subject_pubkey]
                if e.subject_pubkey in seen:
                    continue
                seen.add(e.subject_pubkey)
                # Further-propagation budget for subject:
                #   - One hop has been spent (we're now AT subject), so:
                #       subject can re-endorse (e.depth_allowed - 1) more times,
                #       but also bounded by our remaining (allowed_further - 1).
                next_allowed = min(allowed_further - 1, e.depth_allowed - 1)
                queue.append((
                    e.subject_pubkey,
                    depth + 1,
                    next_allowed,
                    path + [e.subject_pubkey],
                ))
        return None

    def trusted_pubkey_for(
        self,
        agent_id: str,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> Optional[str]:
        """Look up a pubkey trusted (transitively) for this agent_id."""
        # Roots first
        roots = self._load_roots()
        if agent_id in roots:
            return roots[agent_id]
        # Then any non-expired endorsement that targets this agent_id and
        # whose endorser chain is itself rooted within max_depth.
        for e in self._load_all():
            if e.is_expired or e.subject_agent_id != agent_id:
                continue
            # Recursive check: is endorser reachable?
            # Find an endorser agent_id mapping (search roots + endorsements)
            endorser_aid = self._reverse_lookup_agent_id(e.endorser_pubkey)
            if endorser_aid is None:
                continue
            if self.is_trusted(
                endorser_aid, e.endorser_pubkey,
                max_depth=max_depth - 1,
            ):
                return e.subject_pubkey
        return None

    def _reverse_lookup_agent_id(self, pubkey_hex: str) -> Optional[str]:
        for aid, pk in self._load_roots().items():
            if pk == pubkey_hex:
                return aid
        for e in self._load_all():
            if e.is_expired:
                continue
            if e.subject_pubkey == pubkey_hex:
                return e.subject_agent_id
        return None

    # ─── stats ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        all_endorsements = self._load_all()
        active = [e for e in all_endorsements if not e.is_expired]
        return {
            "roots": len(self._load_roots()),
            "endorsements_total": len(all_endorsements),
            "endorsements_active": len(active),
            "unique_subjects": len({e.subject_pubkey for e in active}),
            "unique_endorsers": len({e.endorser_pubkey for e in active}),
        }
