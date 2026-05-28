# Changelog

All notable changes to **NTH DAO** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

## [0.8.0] 鈥?2026-05-25 (Initial extraction)

First standalone release, extracted from `AlexNthLab/hermes-team-agent`
where it was developed in-tree as `team_layer/` + `nth_dao/`.

### Added 鈥?All 8 PRs from the original development branch

#### PR 1: `team_layer/runtime.py` 鈥?Core adapter (250 lines)
- `TeamAgent` lightweight wrapper around any backend
- `TeamMemoryManager` orchestrating multiple providers
- `<memory-context>` fence injection to prevent identity confusion
- `MemoryProviderABC` with 5 lifecycle hooks (init/prefetch/sync_turn/pre_compress/end)

#### PR 2: `team_layer/memory_providers/` 鈥?4 default providers (520 lines)
- `SoulProvider` 鈥?lazy-load TEAM-SOUL.md (<200 tokens)
- `UserModelProvider` 鈥?auto-sediment preferences across sessions
- `VectorProvider` 鈥?skill registry indexer
- `LedgerProvider` 鈥?append-only experience ledger

#### PR 3: `team_layer/compression/` 鈥?5-tier pipeline (330 lines)
- Budget Reduction 鈫?Snip History 鈫?Microcompact 鈫?Collapse 鈫?Auto-summary
- Cheap operators first (zero-cost trims before expensive LLM summarization)
- `preserved-tail` mechanism keeps recent 3 turns at full fidelity

#### PR 4: `team_layer/evolution/` 鈥?EvoLoop self-evolution (1189 lines)
- `EvoTrigger` 鈥?ROI-lagged gate (`count >= 3 AND wasted > budget * 1.5`)
- `Reflector` 鈥?generates fix Patch from failure samples (LLM-optional, template fallback)
- `Verifier` 鈥?subprocess sandbox + Pydantic contract validation (replaces Z3)
- `EvolutionGate` 鈥?auto-merge low-risk, queue high-risk for human review
- Append-only `evolution_audit.jsonl` with full audit trail

#### PR 5: `team_layer/git_sync/` 鈥?Multi-terminal sync (1191 lines)
- `LogCollector` 鈥?zero-collision naming `{host}_{user}_{ts}.jsonl`
- `SkillLoader` 鈥?atomic `git checkout -- skills/` for hot-reload
- `CentralAggregator` 鈥?merges all terminal logs, runs EvoLoop, emits PR-ready report
- GitHub Action `team-evolve-daily.yml` for nightly aggregation

#### PR 6: `team_layer/blackboard/` 鈥?Multi-agent shared workspace (1132 lines)
- 3 scopes: `shared` / `group:<name>` / `private:<agent>`
- Append-only version chains (full history)
- Kanban renderer (TODO/DOING/DONE/BLOCKED)
- CLI: `python -m team_layer.blackboard list|view|post|update|history`
- `BlackboardProvider` injects per-agent task view into system prompt

#### PR 7: `team_layer/backends/` 鈥?AgentBackend ABC (2016 lines)
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

#### PR 8: `nth_dao/` 鈥?Pluggable facade (2096 lines)
- Re-exports all `team_layer` APIs (zero-break compatibility)
- `discovery/` 鈥?heartbeat-based peer discovery + capability search
- `orchestration/` 鈥?Mission/Step relay across sessions and agents
- `attach()` 鈥?one-line integration: `team = nth.attach(agent_id, backend, ...)`
- `TeamSession` facade combining agent + memory + blackboard + discovery + missions
- `INTEGRATION_GUIDE.md` with Hermes / Claude Code / OpenAI examples
- `pyproject.toml` ready for PyPI release

### Verified
- 5 working end-to-end demos in `examples/`
- All demos run zero-deps on stdlib-only Python 3.10+
- 9170+ lines of production code
- Self-improving loop verified: failure 鈫?ledger 鈫?EvoLoop 鈫?skill 鈫?next session prefetch

### Notes
- Originally developed in `AlexNthLab/hermes-team-agent` `team-layer-v1` branch.
- Extracted to this standalone repo to enable `pip install nth-dao`
  for use across any agent framework (not just Hermes).
- Backward compatibility: all original imports (`from team_layer import 鈥)
  still work; recommend new code use `import nth_dao as nth`.

[0.8.1]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.1
[0.8.0]: https://github.com/AlexNthLab/nth-dao/releases/tag/v0.8.0
