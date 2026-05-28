# Nth Team Layer Vision

`nth_team_layer` is not just a multi-agent utility library. Its long-term role
is to become the identity and group layer for a decentralized Agent-to-Agent
collaboration network.

## North Star

The near-term goal is to make Agent identity, group membership, admission, and
collaboration solid. The long-term goal is to provide the foundation for
Agent-to-Agent communication, assistance, transactions, reputation, permissions,
and routing.

Each Agent should have a unique, recognizable, auditable, and authorizable
identity. Each group should be able to act as a decentralized collaboration
node. Group behavior may borrow proven social patterns from QQ, WeChat, and
Telegram while keeping a local-first and offline-capable architecture.

## Current Priorities

Prioritize foundational capabilities before impressive but fragile features:

- Agent identity
- membership and approval
- group and channel primitives
- roles and permissions
- message and announcement flows
- discovery
- append-only audit logs
- simple reputation and trust hints

## Social Product Patterns

The project should learn from basic group behavior in mature social tools:

- join requests
- invitations
- admin approval
- group announcements
- member lists
- group roles
- muting and removal
- direct messages and group messages
- channels and topics
- bot commands
- group files and group tasks

## Decentralized Bias

Avoid prematurely converging on a single central service. Prefer designs that
are:

- file/Git syncable
- local-first
- offline-capable
- mergeable across nodes
- append-only where audit matters
- portable across identities and permission domains

## Quality Bar

Direction alone is not enough. Changes should meet these minimum standards:

- clear API
- stable data structures
- no regression to existing `attach`, discovery, membership, or orchestration
- minimal tests for new behavior
- real permission checks, not decorative fields
- backward-compatible defaults

When reviewing pull requests, prefer changes that advance identity, groups,
admission, permissions, member relationships, and Agent collaboration patterns.
Defer larger marketplace, gossip, or transaction systems until the foundational
layers are stable enough to support them.
