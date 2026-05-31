# Changelog

All notable changes to **NTH DAO** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.9.2] — 2026-05-31 — Revocation, invitations, LAN privacy, protocol spec

### Added — P6 follow-on roadmap items

- **Endorsement revocation** (`web_of_trust.py`):
  - New `Revocation` dataclass — owner-signed cancellation tied to a specific
    `(endorser, subject, issued_at)` triple.
  - `issue_revocation(endorser, endorsement, reason)` mints + signs.
  - `TrustGraph.revoke()` issues + imports in one call;
    `TrustGraph.import_revocation()` validates signature, requires a
    matching endorsement on disk (prevents pre-emptive DoS revocations),
    and dedupes.
  - `_load_all()` now filters out revoked endorsements; chains break
    immediately. Persisted at `team_trust/revocations.jsonl`.
- **Invitation tokens** (`nth_dao/invitation.py`):
  - New `Invitation` dataclass that bundles `team_id` + `owner_pubkey` +
    `join_token` + optional `ws_url` + optional LAN `psk`, signed by owner.
  - `Invitation.mint(team_config, owner_identity, ...)` — fails fast if the
    minting identity doesn't match `team_config.owner_pubkey`.
  - URL form: `nthdao+invite://<base64url payload>`, ≤ 2 KB so it fits
    inside one QR code.
  - `to_qr_terminal()` (qrcode lib only) and `to_qr_png()` (`pip install
    nth-dao[ux]`) render the URL as ASCII / PNG QR code.
  - `Invitation.from_url()` + `validate()` reject expired/tampered/wrong-scheme.
- **LAN discovery privacy** (`discovery/lan.py`):
  - New `psk=` argument on `LANDiscovery`. Both query and hello carry an
    `HMAC-SHA256(psk, nonce)` tag; responder only answers queries with
    matching tag, querier only accepts hellos with matching tag.
    `hmac.compare_digest` used throughout.
  - Empty `psk` = open mode (unchanged behavior, backward compatible).
- **`docs/PROTOCOLS.md`** — 350-line wire-protocol spec covering identity,
  gossip handshake + envelope, endorsements / revocations, signed team
  config, invitations, LAN discovery, marketplace orders, missions. Enables
  Rust / Go / TypeScript implementations to interop with the Python reference.
- **`CONTRIBUTING.md`** "Hard Rules" section documents the eight enforcement
  rules learned from the v0.9.1 review (file I/O via util, signed wire
  protocols, CAS for concurrency, no `except: pass`, etc.).

### Added — extras

- `[lan]` extra: `zeroconf>=0.131` (placeholder for v0.9.3 mDNS backend).
- `[ux]` extra: `qrcode[pil]>=7.4` for invitation QR rendering.

### Tests

- 19 new tests in `tests/test_p6_revoke_psk_invite.py`:
  - revocation round-trip, wrong-issuer rejection, immediate de-trust,
    cross-instance persistence, pre-emptive revocation drop,
    tampered-signature drop;
  - LAN PSK: blocks unauth queries, allows matching, rejects wrong PSK,
    backward-compat open mode;
  - Invitation: mint round-trip, validation, tamper rejection, expiry,
    non-owner mint rejection, wrong-scheme / garbage URL rejection, QR
    terminal rendering, helpful ImportError without extra.
- Total test count now: **102 + 1 skip** (was 83).

## [0.9.1] — 2026-05-31 — P0/P1 security & correctness review fixes

### Security
- **Gossip signature verification fixed**: messages are now verified against the *author's* trusted pubkey (not the relay peer's). Unsigned or mismatched messages are **dropped**, not silently accepted as before. A 10-minute replay window is enforced. `require_signature=True` is the new default.
- **WebSocket challenge-response handshake**: peers must prove possession of their declared pubkey (sign a server-issued nonce) before being added to the peer table. `GossipNode.trust_agent()` + `trusted_pubkeys=` constructor arg manage the trust anchor map; pubkey rotation triggers a `logger.warning`.
- **Atomic mission claim**: `MissionStore.try_claim()` uses cross-process file locks (POSIX `fcntl` / Windows `msvcrt`) plus compare-and-swap, raising `ClaimConflict` when another agent already owns the step. Includes a 6-process race test (`tests/test_concurrent_claim.py`) proving exactly-once semantics.
- **Mission FAILED terminal state**: missions with a failed step and no remaining actionable steps now transition to `FAILED` (previously hung in `ACTIVE` forever).
- **Constant-time join-token comparison**: `MembershipManager` uses `hmac.compare_digest`, preventing timing side-channels.
- **Private key file ACL on Windows**: `AgentIdentity.save()` tightens ACL via `icacls /grant ... /inheritance:r` with a post-write read self-check. Failures emit `logger.warning` instead of `except: pass` leaving keys world-readable.
- **Identity tamper detection**: `AgentIdentity.load()` derives the pubkey from the private key and refuses to load when the stored pubkey disagrees.

### Fixed
- Marketplace `reject()` now notifies the original claimant (replaced broken `hasattr('claimant_history')` always-False check that DMed `"unknown"`).
- Marketplace credits go through `_transfer_credits()` + append-only `*_credits.ledger.jsonl`, with insufficient-balance checks preventing local double-spend.
- Reputation `rate()` validates score range, rejects self-rating, supports `upsert=True` (replace latest), and rate-limits non-upsert calls per `(rater, subject, context)`.
- Reputation `get_all_scores()` / `top_agents()` reuse a single `_load_all()` pass (was O(N²)).
- Channel `fetch()` merges across files by timestamp before slicing `limit` (previously returned host-grouped, mis-ordered results, and `since_msg_id` could miss messages across files).
- Mission `update_step` no longer double-pushes `previous_assignees` when changing status+assignee in one call.
- `Mission.updated_at` is actually bumped on save (was a self-assignment no-op).
- `Mission.from_dict` no longer mutates the caller's dict via `data.pop`.
- `MissionRunner.handoff()` rejects empty targets, self-handoffs, and (when a registry is provided) dead targets.
- `PeerFinder.rank()` adds `min_match` parameter; the old `min_score=0.5` floor returned agents with 0 matching capabilities.
- `AgentRegistry.register()` registers `atexit` only once per instance instead of stacking callbacks across re-attaches.
- `attach()` does the membership check *before* creating the backend, preventing subprocess leaks on join failure; `detach()` calls backend `close/stop/shutdown` if available.
- Gossip `_recv_loop` no longer updates `last_seen` on a throwaway `PeerInfo` object (was a silent no-op).
- Channel `stats()` no longer reuses `_` as both iteration variable and filter predicate.

### Changed
- New `nth_dao/util/io.py` centralises `safe_id`, `atomic_write_json`, `atomic_write_text`, `safe_load_json`, and `InterProcessLock`. Six modules that previously copy-pasted `_safe_id` and atomic-write logic now share this util.
- `__version__` reads from `importlib.metadata` so it stays in sync with `pyproject.toml`.
- `groups.Channel` re-exported as `GroupChannel` alias to disambiguate from `channel.TeamChannel`.

### Added (tests)
- 20 new regression tests in `tests/test_p0_fixes.py` covering atomic claim CAS, mission FAILED state machine, identity tamper rejection, reputation rate-limits / upsert / anti-Sybil credits, marketplace double-spend prevention, channel fetch global sort, peer finder min_match, and handoff alive-check.
- Cross-process race test `tests/test_concurrent_claim.py` (spawn 6 workers → exactly 1 winner, 5 ClaimConflict).
- Total test count: 45 (up from 23).

### P5 — "People nearby" discovery (WeChat-style find-and-meet)

- **Fuzzy `PeerFinder.search(query)`** — substring / prefix / exact-match scoring across `agent_id`, `label` (extracted from `metadata["identity"]["label"]`), `capabilities`, and `groups`. Returns `MatchResult` list ranked by score, with `+0.5` idle bonus and configurable `limit` / `min_score` / `exclude_agent_ids` / `fields`. 10 new regression tests covering each scoring rule, label extraction, idle tie-breaking, self-exclusion, and limit.
- **`nth_dao/discovery/lan.py`** — zero-config UDP-based agent discovery on the local subnet. No mDNS / Bonjour, pure stdlib `socket`.
  - `LANDiscovery(agent_id, label, capabilities, ws_url, pubkey_hex, port=9877)` — call `.start()` to spawn a background responder thread that answers query packets that match its capabilities.
  - `LANDiscovery.discover(timeout=3.0, wanted_capabilities=None, target_addrs=None)` — broadcast a query (`255.255.255.255` by default; tests use `127.0.0.1`) and collect `LANPeer` responses for `timeout` seconds. De-duped by agent_id; results carry `ws_url` so the caller can hand them to `GossipNode.connect()` and `pubkey_hex` so they can be added to `TrustGraph.add_root()`.
  - Context-manager friendly (`with LANDiscovery(...) as lan: ...`).
  - Nonce-based response matching so stale replies are dropped.
  - Windows hardening: catches `ConnectionResetError` from ICMP "port unreachable" bleed-through that would otherwise blow up `recvfrom`.
- **`__init__.py` facade exports** `LANDiscovery` + `LANPeer`.
- 7 new LAN tests covering: peer found, capability filter, self-exclusion (loopback), empty result when nobody's listening, stale-nonce rejection, facade exports, context manager lifecycle.
- Total test count now: **83** (up from 66).

### P4 — Web-of-Trust: endorsement-based multi-hop trust

- **New module `nth_dao/web_of_trust.py`** with `Endorsement` dataclass, `TrustGraph` resolver, and `issue_endorsement()` helper.
- An `Endorsement` is a signed `{endorser_pubkey, subject_pubkey, subject_agent_id, depth_allowed, context, issued_at, expires_at, sig}` payload. `endorser.sign_json(endorsement.signable_dict())` produces the signature.
- `TrustGraph` stores root anchors in `team_trust/roots.json` and endorsements append-only in `team_trust/endorsements.jsonl`. `is_trusted(agent_id, pubkey, max_depth=2)` runs a bounded BFS, verifying the signature chain and respecting each endorser's `depth_allowed` cap.
- `GossipNode(..., trust_graph=..., wot_max_depth=2)` now falls back to the trust graph when an author's pubkey isn't directly pinned. Log lines tag whether trust came from `pinned` or `wot`.
- **Hop budget enforced correctly**: an endorsement with `depth_allowed=1` makes the subject a *leaf* — their own endorsements won't propagate further. Verified by `test_depth_allowed_caps_propagation`.
- 16 new tests in `tests/test_web_of_trust.py` covering: round-trip + sig verify, tampered endorsement, root direct trust, agent_id spoof rejection, 1-hop / 2-hop chains, expired endorsements, wrong-signer endorsements, `resolve_path` returns full chain, depth_allowed cap, invalid depth at issue, `trusted_pubkey_for` lookup, cross-instance persistence, facade re-exports, and gossip integration.
- Total test count now: **66** (up from 50).

### P3 — anti-tamper hardening

- **Signed team config (anti git-sync poisoning)**: `TeamConfig` gains `owner_pubkey` + `owner_sig` + `sig_updated_at` fields. When `MembershipManager` is constructed with an `owner_identity` that holds Ed25519 keys, every `save_config()` signs the canonical config. `load_config()` rejects (returns empty `TeamConfig()`) any signed file whose signature doesn't verify against its pinned `owner_pubkey` — meaning a malicious node can't `git push` a tampered `team.json` that adds themselves as admin.
- `MembershipManager.enable_signed_owner(identity, actor_id)` — admin-gated upgrade path to turn signing on for an existing unsigned team.
- `init_team(..., owner_identity=...)` — bootstrap a fresh team with owner signing in one shot.
- `attach()` auto-detects whether the agent is the legitimate owner (its pubkey matches the file's `owner_pubkey`) and only then activates owner-signing for that session — non-owner attaches read-only verify.
- **Reputation credits scoped by pubkey fingerprint**: when an `AgentIdentity` with crypto is passed, the anti-Sybil credit file uses `identity.fingerprint()[:16]` rather than `safe_id(agent_id)`. An attacker spawning 1000 `agent_id`s now still shares 5 starting credits per unique keypair (instead of 5 × 1000); legitimate single-keypair-multi-agent_id setups now correctly share one credit pool.

### P2 polish
- **Anti-Sybil rating credits** in `ReputationManager`: each new (non-upsert) `rate()` costs 1 credit; initial balance 5, daily refill +3 (cap 30). `rep.credits()` exposes current balance. Combined with the (rater, subject, context) rate-limit, this makes mass-rating costly without needing a global ledger.
- **Backend resource release**: `attach.detach()` now uses an explicit `_close_backend()` helper that probes `close` → `stop` → `shutdown` (in that priority order) and logs at debug when none exist.
- **Docstring de-mojibake**: replaced encoding-corrupted Chinese comments / docstrings in `nth_dao/__init__.py`, `nth_dao/attach.py`, `nth_dao/discovery/agent_registry.py`, `nth_dao/orchestration/mission.py`, `nth_dao/orchestration/mission_store.py`, and `nth_dao/orchestration/mission_runner.py` with English equivalents that match the actual behavior. (The old files contained `""`-only docstrings after the encoding loss.)
- `AgentRecord.short()` no longer uses empty strings as alive/dead markers (replaced with `*`/`-`).
- `agent_registry` heartbeat-tick failure is now logged at debug instead of silently swallowed.

## [0.9.0] - 2026-05-28

### Added
- Added a minimal `nth_dao.web` local console with membership-gated group chat,
  announcements, tasks, and audit-backed state APIs.
- Published the NTH DAO manifesto as the public mission statement.
- Completed the hard rename to **NTH DAO** / `nth_dao` and removed the old public package name.
- Added `AgentIdentity` / `AgentID` identity primitives with optional Ed25519 signing via the `crypto` extra.
- Added `attach(..., identity=...)` support and discovery metadata export without bypassing membership approval.
- Added identity tests for persistence, attach metadata, and membership-gated identity use.
- Added `VISION.md` and `CONTRIBUTING.md` to document the decentralized Agent-to-Agent identity/group direction and review bar.
- Added basic `TeamRole` role/permission hints for owner, admin, and member flows.
- Added a local-first `GroupManager` with `Channel`, `Message`, `Announcement`, `Task`, `AuditEvent`, and `TrustHint` primitives.
- Added `MembershipRequest` as a semantic alias for `JoinRequest`.

## [0.8.1] - 2026-05-27

### Fixed
- Made membership approval an actual `attach()` gate before heartbeat/discovery registration.
- Added admin checks for approval, rejection, invites, member removal, admin changes, and policy changes.
- Accepted string join policies such as `"approval"` in addition to `JoinPolicy` enum values.
- Ensured open/token joins persist approved agents into `team.json` member state.
- Cleaned the membership module encoding/comments and removed trailing whitespace issues.

### Added
- Added focused membership tests for approval blocking, admin approval, open joins, token joins, and string policy compatibility.
- Added test path setup for `examples/` bot imports and cleanup for generated test `team.json`.

## [0.8.0] - 2026-05-25 (Initial extraction)

First standalone release, extracted from `AlexNthLab/hermes-team-agent`
where it was developed in-tree as `team_layer/` + `nth_dao/`.

### Added - All 8 PRs from the original development branch

#### PR 1: `team_layer/runtime.py` - Core adapter (250 lines)
- `TeamAgent` lightweight wrapper around any backend
- `TeamMemoryManager` orchestrating multiple providers
- `<memory-context>` fence injection to prevent identity confusion
- `MemoryProviderABC` with 5 lifecycle hooks (init/prefetch/sync_turn/pre_compress/end)

#### PR 2: `team_layer/memory_providers/` - 4 default providers (520 lines)
- `SoulProvider` - lazy-load TEAM-SOUL.md (<200 tokens)
- `UserModelProvider` - auto-sediment preferences across sessions
- `VectorProvider` - skill registry indexer
- `LedgerProvider` - append-only experience ledger

#### PR 3: `team_layer/compression/` - 5-tier pipeline (330 lines)
- Budget Reduction -> Snip History -> Microcompact -> Collapse -> Auto-summary
- Cheap operators first (zero-cost trims before expensive LLM summarization)
- `preserved-tail` mechanism keeps recent 3 turns at full fidelity

#### PR 4: `team_layer/evolution/` - EvoLoop self-evolution (1189 lines)
- `EvoTrigger` - ROI-lagged gate (`count >= 3 AND wasted > budget * 1.5`)
- `Reflector` - generates fix Patch from failure samples (LLM-optional, template fallback)
- `Verifier` - subprocess sandbox + Pydantic contract validation (replaces Z3)
- `EvolutionGate` - auto-merge low-risk, queue high-risk for human review
- Append-only `evolution_audit.jsonl` with full audit trail

#### PR 5: `team_layer/git_sync/` - Multi-terminal sync (1191 lines)
- `LogCollector` - zero-collision naming `{host}_{user}_{ts}.jsonl`
- `SkillLoader` - atomic `git checkout -- skills/` for hot-reload
- `CentralAggregator` - merges all terminal logs, runs EvoLoop, emits PR-ready report
- GitHub Action `team-evolve-daily.yml` for nightly aggregation

#### PR 6: `team_layer/blackboard/` - Multi-agent shared workspace (1132 lines)
- 3 scopes: `shared` / `group:<name>` / `private:<agent>`
- Append-only version chains (full history)
- Kanban renderer (TODO/DOING/DONE/BLOCKED)
- CLI: `python -m team_layer.blackboard list|view|post|update|history`
- `BlackboardProvider` injects per-agent task view into system prompt

#### PR 7: `team_layer/backends/` - AgentBackend ABC (2016 lines)
- Unified interface for any agent framework
- 6 built-in backends:
  - `mock` (always available, deterministic)
  - `hermes` (subprocess)
  - `claude_code` (stream-json CLI)
  - `openclaw` (ACP HTTP)
  - `codex` (OpenAI Codex CLI)
  - `openhands` (REST API)
- `BackendRegistry` with availability probing
- `TeamAgent.run_with_backend()` for backend-driven main loop

#### PR 8: `nth_dao/` - Pluggable facade (2096 lines)
- Re-exports the core DAO/group APIs through the `nth_dao` public package
- `discovery/` - heartbeat-based peer discovery + capability search
- `orchestration/` - Mission/Step relay across sessions and agents
- `attach()` - one-line integration: `team = nth.attach(agent_id, backend, ...)`
- `TeamSession` facade combining agent + memory + blackboard + discovery + missions
- `INTEGRATION_GUIDE.md` with Hermes / Claude Code / OpenAI examples
- `pyproject.toml` ready for PyPI release

### Verified
- 5 working end-to-end demos in `examples/`
- All demos run zero-deps on stdlib-only Python 3.10+
- 9170+ lines of production code
- Self-improving loop verified: failure -> ledger -> EvoLoop -> skill -> next session prefetch

### Notes
- Extracted to this standalone repo to enable `pip install nth-dao`
  for use across agent frameworks and local-first DAO nodes.
- New integrations should use `import nth_dao as nth`; the former
  `nth_team_layer` public package is intentionally removed.

[0.9.2]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.9.2
[0.9.1]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.9.1
[0.9.0]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.9.0
[0.8.1]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.1
[0.8.0]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.0
