# Contributing

Contributions should move NTH DAO toward an AI-native Web3 DAO protocol layer
for humans and agents. See [VISION.md](VISION.md) for the product direction and
review priorities.

## Review Priorities

Prefer small, well-tested changes that strengthen:

- Agent and human identity
- DAO membership and admission
- groups, channels, and topics
- roles and permissions
- messages and announcements
- tasks, missions, and handoff
- discovery and append-only audit logs
- simple trust and reputation hints
- local-first, Git-syncable data formats

Large features such as P2P gossip, marketplaces, settlement, wallet flows,
token-gating, or global reputation are welcome as proposals, but should not
bypass the foundational identity, membership, permission, and audit layers.

## Required Quality Bar

Before a change is merged, it should:

- keep existing public APIs backward compatible unless a migration is explicit
- preserve attach, discovery, membership, and orchestration behavior
- include focused tests for new behavior and permission boundaries
- use local-first, file-backed, mergeable data when possible
- avoid introducing a required central service
- avoid adding required third-party dependencies to the core package

Optional dependencies should be exposed through extras such as `crypto`, `web`,
or future transport-specific extras.

## Naming Direction

The project name and forward import path are NTH DAO and `nth_dao`.

The historical `nth_team_layer` import path remains as a compatibility layer
while the codebase migrates. New examples, docs, and integrations should prefer:

```python
import nth_dao as nth
```

## Pull Request Checklist

- Does this change support the NTH DAO vision in `VISION.md`?
- Does it preserve membership approval and role/permission checks?
- Is every new persisted format documented by clear field names?
- Can files be synced or merged across decentralized nodes?
- Are failure modes explicit rather than silently ignored?
- Are tests included for the core behavior?
