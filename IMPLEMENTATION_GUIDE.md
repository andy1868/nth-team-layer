# NTH DAO Implementation Guide

NTH DAO is a local-first identity, membership, and collaboration layer for
humans and agents. This guide describes the current foundation and the next
engineering priorities.

## Current Foundation

- `nth_dao.identity` defines portable agent identities and optional signatures.
- `nth_dao.membership` controls admission, roles, permissions, and approval.
- `nth_dao.groups` stores DAO members, channels, messages, announcements,
  tasks, audit events, and trust hints.
- `nth_dao.discovery` lets agents publish presence and discover peers.
- `nth_dao.orchestration` routes long-running missions across agents.
- `team_layer` remains an internal runtime package for memory, blackboard,
  backend adapters, compression, evolution, and Git sync.

## Design Priorities

NTH DAO should keep the protocol small, inspectable, and portable:

- local-first storage
- plain files where possible
- append-only audit trails where accountability matters
- mergeable state across nodes
- no mandatory central service
- clear permission checks before privileged actions
- stable public APIs under `nth_dao`

## Minimal Integration

```python
import nth_dao as nth

with nth.attach(
    agent_id="builder-1",
    backend="mock",
    capabilities=["python", "review"],
    groups=["core"],
) as dao:
    peers = dao.discover_others()
    mission = dao.start_mission(
        title="ship protocol draft",
        goal="prepare the first DAO collaboration protocol draft",
        steps=[
            {"id": "spec", "description": "write the protocol spec"},
            {"id": "review", "description": "review permission and audit logic"},
        ],
    )
```

## Group Layer

The group layer should support the social primitives users already understand:

- join requests
- invitations
- admin approval
- member lists
- roles and permissions
- channels and topics
- direct messages and group messages
- announcements
- tasks
- audit logs
- trust hints

These features should be implemented without forcing every DAO into a single
hosted platform.

## Release Rule

Public documentation and release notes are English-only. Experimental notes,
private drafts, and local work may exist during development, but anything
published to the repository should represent the NTH DAO mission clearly.
