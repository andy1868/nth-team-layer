"""GroupRegistry — globally-unique, discoverable group/DAO names with governance.

Solves three things the v0.9.5 GroupManager left open:

  1. **Name uniqueness within a workspace**: a workspace can't have two
     groups named "frontend". Names normalize to a slug + collide-or-fail.
  2. **Cross-workspace discovery**: a group records its `team_id` + owner
     pubkey + creation timestamp. When workspaces share via git_sync, a
     name collision across workspaces resolves to the *first published*
     (oldest created_at + lowest pubkey hash as tiebreaker).
  3. **Group governance**: open / closed / approval / vote policy, with
     a vote-based policy-change mechanism so the founder doesn't unilaterally
     own the group forever.

Wire format on disk:

    team_groups/
    ├── <slug>.json                # one signed group record per file
    ├── policy_votes/<vote_id>.json # signed policy change proposals + votes
    └── _index.json                # derived: { slug → group_id }

A group record is signed by the founder at creation, then re-signed (with
a new sig) when its policy or membership changes. Earlier signed snapshots
remain in git history.

The policy state machine:

    open       — anyone can join, anyone can post
    approval   — anyone can request; an admin admits
    closed     — invite-only; admins add members directly
    voted      — any admission requires a majority vote of current members

A policy change goes through the same vote: PolicyChangeProposal signed
by the proposer, members vote, threshold is `> 50%` of current member
count (founders cannot override).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .identity import AgentIdentity, _NACL_AVAILABLE, _VerifyKey, canonical_json
from .util import atomic_write_json, safe_id, safe_load_json

logger = logging.getLogger("nth_dao.group_registry")


class GroupPolicy(str, Enum):
    OPEN     = "open"
    APPROVAL = "approval"
    CLOSED   = "closed"
    VOTED    = "voted"


class GroupRegistryError(Exception):
    """Raised on name collisions, signature errors, governance violations."""


# ─────────────────── slug normalization ───────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MIN_SLUG_LEN = 3
_MAX_SLUG_LEN = 40


def normalize_group_name(name: str) -> str:
    """Turn a human group name into a slug suitable for uniqueness:

        "Frontend Team!"  →  "frontend-team"
        "DAO 测试 0.9"     →  "dao-0-9"     (non-ASCII letters drop; digits stay)

    Raises GroupRegistryError if the result is too short/long.
    """
    if not isinstance(name, str):
        raise GroupRegistryError("name must be a string")
    s = name.strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) < _MIN_SLUG_LEN:
        raise GroupRegistryError(
            f"group name slug must be at least {_MIN_SLUG_LEN} characters; "
            f"got {s!r}"
        )
    if len(s) > _MAX_SLUG_LEN:
        raise GroupRegistryError(
            f"group name slug must be at most {_MAX_SLUG_LEN} characters"
        )
    return s


# ─────────────────── data types ───────────────────


@dataclass
class GroupRecord:
    """One signed group record, the canonical answer to "what is this group?"."""

    group_id: str
    slug: str
    display_name: str
    description: str
    policy: GroupPolicy
    founder_pubkey: str
    member_pubkeys: List[str] = field(default_factory=list)
    admin_pubkeys: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Signature is over signable_dict() — produced by the founder at create
    # time and refreshed by whoever has admin permission on policy/member
    # changes. The pubkey that produced `sig` lives in `signer_pubkey`.
    signer_pubkey: str = ""
    sig: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["policy"] = self.policy.value if isinstance(self.policy, GroupPolicy) else self.policy
        return d

    def signable_dict(self) -> dict:
        d = self.to_dict()
        d.pop("sig", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "GroupRecord":
        policy_raw = data.get("policy", "open")
        try:
            policy = GroupPolicy(policy_raw)
        except ValueError:
            policy = GroupPolicy.OPEN
        return cls(
            group_id=data.get("group_id", ""),
            slug=data.get("slug", ""),
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            policy=policy,
            founder_pubkey=data.get("founder_pubkey", ""),
            member_pubkeys=list(data.get("member_pubkeys", [])),
            admin_pubkeys=list(data.get("admin_pubkeys", [])),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            metadata=dict(data.get("metadata", {})),
            signer_pubkey=data.get("signer_pubkey", ""),
            sig=data.get("sig", ""),
        )

    def verify_signature(self) -> bool:
        if not (_NACL_AVAILABLE and _VerifyKey and self.sig and self.signer_pubkey):
            return False
        try:
            _VerifyKey(bytes.fromhex(self.signer_pubkey)).verify(
                canonical_json(self.signable_dict()),
                bytes.fromhex(self.sig),
            )
            return True
        except Exception:
            return False


@dataclass
class PolicyChangeProposal:
    """A signed proposal to change a group's policy / membership.

    Members vote by appending a signed `Vote` to the proposal's `votes` list.
    Resolves when:

        sum(weight of "yes" votes) > 0.5 * len(group.member_pubkeys)

    (One pubkey = one vote, regardless of how many `agent_id`s it has.)
    """

    proposal_id: str
    group_id: str
    proposer_pubkey: str
    proposed_policy: GroupPolicy
    proposed_add_members: List[str] = field(default_factory=list)
    proposed_remove_members: List[str] = field(default_factory=list)
    proposed_display_name: Optional[str] = None
    rationale: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expires_at: str = ""
    votes: List[Dict[str, Any]] = field(default_factory=list)
    proposer_sig: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["proposed_policy"] = (
            self.proposed_policy.value
            if isinstance(self.proposed_policy, GroupPolicy)
            else self.proposed_policy
        )
        return d

    def signable_dict(self) -> dict:
        """Proposer's signature covers the proposal *excluding* the votes list."""
        d = self.to_dict()
        d.pop("votes", None)
        d.pop("proposer_sig", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyChangeProposal":
        try:
            policy = GroupPolicy(data.get("proposed_policy", "open"))
        except ValueError:
            policy = GroupPolicy.OPEN
        return cls(
            proposal_id=data.get("proposal_id", ""),
            group_id=data.get("group_id", ""),
            proposer_pubkey=data.get("proposer_pubkey", ""),
            proposed_policy=policy,
            proposed_add_members=list(data.get("proposed_add_members", [])),
            proposed_remove_members=list(data.get("proposed_remove_members", [])),
            proposed_display_name=data.get("proposed_display_name"),
            rationale=data.get("rationale", ""),
            created_at=data.get("created_at", ""),
            expires_at=data.get("expires_at", ""),
            votes=list(data.get("votes", [])),
            proposer_sig=data.get("proposer_sig", ""),
        )

    def verify_proposer_signature(self) -> bool:
        if not (_NACL_AVAILABLE and _VerifyKey
                and self.proposer_sig and self.proposer_pubkey):
            return False
        try:
            _VerifyKey(bytes.fromhex(self.proposer_pubkey)).verify(
                canonical_json(self.signable_dict()),
                bytes.fromhex(self.proposer_sig),
            )
            return True
        except Exception:
            return False

    def count_yes_votes(self, eligible_member_pubkeys: List[str]) -> int:
        """How many *distinct* eligible-member yes-votes have valid sigs."""
        if not (_NACL_AVAILABLE and _VerifyKey):
            return 0
        eligible = set(eligible_member_pubkeys)
        seen: set = set()
        for v in self.votes:
            voter = v.get("voter_pubkey", "")
            if voter in seen or voter not in eligible:
                continue
            if v.get("choice") != "yes":
                continue
            try:
                _VerifyKey(bytes.fromhex(voter)).verify(
                    canonical_json({
                        "proposal_id": self.proposal_id,
                        "choice": "yes",
                        "voted_at": v.get("voted_at", ""),
                    }),
                    bytes.fromhex(v.get("sig", "")),
                )
                seen.add(voter)
            except Exception:
                continue
        return len(seen)


# ─────────────────── helpers ───────────────────


def create_group(
    founder: AgentIdentity,
    *,
    display_name: str,
    description: str = "",
    policy: Union[GroupPolicy, str] = GroupPolicy.OPEN,
    initial_admin_pubkeys: Optional[List[str]] = None,
) -> GroupRecord:
    """Create + sign a fresh GroupRecord (not yet persisted)."""
    if not getattr(founder, "can_sign", False):
        raise GroupRegistryError("create_group requires a signing-capable founder")
    if isinstance(policy, str):
        try:
            policy = GroupPolicy(policy)
        except ValueError:
            raise GroupRegistryError(f"unknown policy: {policy!r}")
    slug = normalize_group_name(display_name)
    admins = list(initial_admin_pubkeys or [])
    if founder.pubkey_hex not in admins:
        admins.append(founder.pubkey_hex)
    record = GroupRecord(
        group_id=uuid.uuid4().hex[:12],
        slug=slug,
        display_name=display_name,
        description=description,
        policy=policy,
        founder_pubkey=founder.pubkey_hex,
        member_pubkeys=list(set(admins)),
        admin_pubkeys=admins,
        signer_pubkey=founder.pubkey_hex,
    )
    record.sig = founder.sign_json(record.signable_dict())
    return record


def propose_policy_change(
    proposer: AgentIdentity,
    group: GroupRecord,
    *,
    new_policy: Optional[Union[GroupPolicy, str]] = None,
    add_members: Optional[List[str]] = None,
    remove_members: Optional[List[str]] = None,
    new_display_name: Optional[str] = None,
    rationale: str = "",
    ttl_days: int = 7,
) -> PolicyChangeProposal:
    """A current group member proposes a policy or membership change."""
    if not getattr(proposer, "can_sign", False):
        raise GroupRegistryError("propose_policy_change requires a signing identity")
    if proposer.pubkey_hex not in group.member_pubkeys:
        raise GroupRegistryError("only current members may propose changes")
    if new_policy is not None and isinstance(new_policy, str):
        try:
            new_policy = GroupPolicy(new_policy)
        except ValueError:
            raise GroupRegistryError(f"unknown policy: {new_policy!r}")
    from datetime import timedelta
    expires = (datetime.now() + timedelta(days=ttl_days)).isoformat()
    proposal = PolicyChangeProposal(
        proposal_id=uuid.uuid4().hex[:12],
        group_id=group.group_id,
        proposer_pubkey=proposer.pubkey_hex,
        proposed_policy=(new_policy if new_policy is not None else group.policy),
        proposed_add_members=list(add_members or []),
        proposed_remove_members=list(remove_members or []),
        proposed_display_name=new_display_name,
        rationale=rationale,
        expires_at=expires,
    )
    proposal.proposer_sig = proposer.sign_json(proposal.signable_dict())
    return proposal


def cast_vote(
    voter: AgentIdentity,
    proposal: PolicyChangeProposal,
    *,
    choice: str = "yes",
) -> Dict[str, Any]:
    """A member votes on a proposal. Append the returned dict to proposal.votes."""
    if not getattr(voter, "can_sign", False):
        raise GroupRegistryError("cast_vote requires a signing identity")
    if choice not in ("yes", "no", "abstain"):
        raise GroupRegistryError("choice must be yes / no / abstain")
    voted_at = datetime.now().isoformat()
    payload = {"proposal_id": proposal.proposal_id, "choice": choice, "voted_at": voted_at}
    sig = voter.sign_json(payload)
    return {
        "voter_pubkey": voter.pubkey_hex,
        "choice": choice,
        "voted_at": voted_at,
        "sig": sig,
    }


def resolve_proposal(
    proposal: PolicyChangeProposal,
    group: GroupRecord,
) -> Tuple[bool, str]:
    """Has the proposal passed?

    Returns (passed, reason). passed=True only when:
      1. proposer signature verifies
      2. proposal not expired
      3. > 50% of current group members voted "yes" with valid sigs
    """
    if not proposal.verify_proposer_signature():
        return False, "proposer signature invalid"
    if proposal.expires_at:
        try:
            if datetime.now() > datetime.fromisoformat(proposal.expires_at):
                return False, "proposal expired"
        except ValueError:
            pass
    yes = proposal.count_yes_votes(group.member_pubkeys)
    need = (len(group.member_pubkeys) // 2) + 1
    if yes >= need:
        return True, f"passed ({yes}/{len(group.member_pubkeys)} yes; need {need})"
    return False, f"insufficient yes votes ({yes}/{len(group.member_pubkeys)}; need {need})"


def apply_proposal(
    signer: AgentIdentity,
    proposal: PolicyChangeProposal,
    group: GroupRecord,
) -> GroupRecord:
    """Build a new GroupRecord reflecting the passed proposal.

    `signer` is the identity re-signing the updated record. SHOULD be an
    admin pubkey already in `group.admin_pubkeys`.
    """
    passed, reason = resolve_proposal(proposal, group)
    if not passed:
        raise GroupRegistryError(f"proposal not passed: {reason}")
    if signer.pubkey_hex not in group.admin_pubkeys:
        raise GroupRegistryError("only an admin may apply a passed proposal")
    new_members = set(group.member_pubkeys)
    for pk in proposal.proposed_add_members:
        new_members.add(pk)
    for pk in proposal.proposed_remove_members:
        new_members.discard(pk)
    updated = GroupRecord(
        group_id=group.group_id,
        slug=group.slug,
        display_name=(
            proposal.proposed_display_name
            if proposal.proposed_display_name is not None
            else group.display_name
        ),
        description=group.description,
        policy=proposal.proposed_policy,
        founder_pubkey=group.founder_pubkey,
        member_pubkeys=sorted(new_members),
        admin_pubkeys=list(group.admin_pubkeys),
        created_at=group.created_at,
        updated_at=datetime.now().isoformat(),
        metadata=dict(group.metadata),
        signer_pubkey=signer.pubkey_hex,
    )
    updated.sig = signer.sign_json(updated.signable_dict())
    return updated


# ─────────────────── persistence ───────────────────


class GroupRegistry:
    """Workspace-local registry of signed groups + their proposals.

    Layout:
        team_groups/
        ├── <slug>.json
        ├── _index.json
        └── policy_votes/<proposal_id>.json
    """

    SUBDIR = "team_groups"
    INDEX_NAME = "_index.json"
    VOTES_SUBDIR = "policy_votes"

    def __init__(self, workspace: Union[str, Path]):
        self.workspace = Path(workspace)
        self.base = self.workspace / self.SUBDIR
        self.base.mkdir(parents=True, exist_ok=True)
        self.votes_dir = self.base / self.VOTES_SUBDIR
        self.votes_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base / self.INDEX_NAME

    def _path_for_slug(self, slug: str) -> Path:
        return self.base / f"{safe_id(slug)}.json"

    # ── publish / load ──

    def publish(self, record: GroupRecord) -> Path:
        """Persist a signed record. Enforces slug uniqueness.

        Two semantics:
          - Brand-new slug: succeed.
          - Existing slug, SAME group_id: overwrite (treated as update).
          - Existing slug, DIFFERENT group_id: GroupRegistryError.
        """
        if not record.verify_signature():
            raise GroupRegistryError("refuse to publish: signature invalid")
        path = self._path_for_slug(record.slug)
        existing = safe_load_json(path, fallback=None)
        if existing and isinstance(existing, dict):
            if existing.get("group_id") != record.group_id:
                raise GroupRegistryError(
                    f"slug {record.slug!r} already taken by another group "
                    f"(group_id={existing.get('group_id')})"
                )
        atomic_write_json(path, record.to_dict())
        self._refresh_index()
        return path

    def load_by_slug(self, slug_or_name: str) -> Optional[GroupRecord]:
        try:
            slug = normalize_group_name(slug_or_name)
        except GroupRegistryError:
            slug = slug_or_name.lower()
        data = safe_load_json(self._path_for_slug(slug), fallback=None)
        if data is None:
            return None
        try:
            return GroupRecord.from_dict(data)
        except Exception:
            return None

    def load_by_id(self, group_id: str) -> Optional[GroupRecord]:
        for rec in self.list_all():
            if rec.group_id == group_id:
                return rec
        return None

    def list_all(self) -> List[GroupRecord]:
        out: List[GroupRecord] = []
        if not self.base.exists():
            return out
        for path in sorted(self.base.glob("*.json")):
            if path.name == self.INDEX_NAME:
                continue
            data = safe_load_json(path, fallback=None)
            if data is None:
                continue
            try:
                out.append(GroupRecord.from_dict(data))
            except Exception:
                continue
        return out

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        policy: Optional[GroupPolicy] = None,
    ) -> List[GroupRecord]:
        """Fuzzy search on slug, display_name, description.

        WeChat/QQ-style: substring + prefix prioritized.
        """
        q = query.strip().lower()
        if not q:
            return []
        scored: List[Tuple[float, GroupRecord]] = []
        for r in self.list_all():
            if policy is not None and r.policy != policy:
                continue
            score = 0.0
            for field_name in (r.slug, r.display_name.lower(), r.description.lower()):
                if not field_name:
                    continue
                if field_name == q:
                    score += 3.0
                elif field_name.startswith(q):
                    score += 1.5
                elif q in field_name:
                    score += 0.8
            if score >= 0.5:
                scored.append((score, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [r for _, r in scored[:limit]]

    # ── proposals ──

    def save_proposal(self, proposal: PolicyChangeProposal) -> Path:
        path = self.votes_dir / f"{safe_id(proposal.proposal_id)}.json"
        atomic_write_json(path, proposal.to_dict())
        return path

    def load_proposal(self, proposal_id: str) -> Optional[PolicyChangeProposal]:
        path = self.votes_dir / f"{safe_id(proposal_id)}.json"
        data = safe_load_json(path, fallback=None)
        if data is None:
            return None
        try:
            return PolicyChangeProposal.from_dict(data)
        except Exception:
            return None

    def list_proposals_for(self, group_id: str) -> List[PolicyChangeProposal]:
        out: List[PolicyChangeProposal] = []
        for path in sorted(self.votes_dir.glob("*.json")):
            data = safe_load_json(path, fallback=None)
            if data is None:
                continue
            try:
                p = PolicyChangeProposal.from_dict(data)
            except Exception:
                continue
            if p.group_id == group_id:
                out.append(p)
        return out

    # ── index ──

    def _refresh_index(self) -> None:
        idx: Dict[str, Dict[str, str]] = {}
        for rec in self.list_all():
            idx[rec.slug] = {
                "group_id": rec.group_id,
                "display_name": rec.display_name,
                "policy": rec.policy.value if isinstance(rec.policy, GroupPolicy) else rec.policy,
                "founder_pubkey": rec.founder_pubkey,
                "members": str(len(rec.member_pubkeys)),
            }
        atomic_write_json(self.index_path, {
            "generated_at": datetime.now().isoformat(),
            "groups": idx,
        })

    def load_index(self) -> Dict[str, Any]:
        return safe_load_json(self.index_path, fallback={}) or {}
