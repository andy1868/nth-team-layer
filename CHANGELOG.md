# Changelog

All notable changes to **NTH DAO** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.9.0] - 2026-05-28

### Added
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

[0.9.0]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.9.0
[0.8.1]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.1
[0.8.0]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.0
