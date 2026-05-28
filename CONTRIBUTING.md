# Contributing

Contributions should move `nth_team_layer` toward a decentralized Agent-to-Agent
identity and group protocol layer. See [VISION.md](VISION.md) for the product
direction and review priorities.

## Review Priorities

Prefer small, well-tested changes that strengthen:

- Agent identity
- membership and approval
- groups, channels, and topics
- roles and permissions
- messages and announcements
- discovery and audit logs
- simple trust and reputation hints

Large features such as P2P gossip, marketplaces, settlement, or global
reputation are welcome as proposals, but should not bypass the foundational
identity, membership, permission, and audit layers.

## Required Quality Bar

Before a change is merged, it should:

- keep existing public APIs backward compatible unless a migration is explicit
- preserve `attach`, discovery, membership, and orchestration behavior
- include focused tests for new behavior and permission boundaries
- use local-first, file-backed, mergeable data when possible
- avoid introducing a required central service
- avoid adding required third-party dependencies to the core package

Optional dependencies should be exposed through extras such as `crypto`, `web`,
or future transport-specific extras.

## Pull Request Checklist

- Does this change support the vision in `VISION.md`?
- Does it preserve membership approval and role/permission checks?
- Is every new persisted format documented by clear field names?
- Can files be synced or merged across decentralized nodes?
- Are failure modes explicit rather than silently ignored?
- Are tests included for the core behavior?
