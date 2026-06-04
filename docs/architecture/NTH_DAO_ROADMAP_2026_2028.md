# NTH DAO Strategic Roadmap (2026 H2 — 2028)

**Status**: maintainer-approved architecture proposal
**Author**: NTH DAO architecture team
**As of**: June 2026 (v0.9.8 shipped)
**Companion docs**:
- [`docs/research/AGENT_COLLABORATION_2026.md`](../research/AGENT_COLLABORATION_2026.md) — landscape survey
- [`docs/research/A2A_ALIGNMENT.md`](../research/A2A_ALIGNMENT.md) — A2A side-by-side
- [`docs/PROTOCOLS.md`](../PROTOCOLS.md) — wire-format spec

---

## 0 — Operating principles (non-negotiable across all horizons)

These four principles trump roadmap convenience. Anything proposed below
that violates them is rejected before it gets a ticket.

| # | Principle | Why it stays |
|---|-----------|--------------|
| **P1** | **Local-first is the default.** A DAO must run on a private LAN with zero blockchain, zero internet, zero KYC. Every external integration is opt-in. | This is what differentiates us from Olas / Virtuals / Skyfire. They make on-chain mandatory; we make it optional. |
| **P2** | **TS for UI, Python for everything else.** Browser extension, dashboards, web console, mobile companion ⇒ TypeScript. Protocol, backends, CLI, tests, examples ⇒ Python. The Iron Rule. | Long-term operability: separates concerns by where the language ecosystem shines, not by who wrote it first. Logged in `MEMORY.md`. |
| **P3** | **No NTH token.** Settlement uses external rails (USDC via x402, fiat via AP2, anything via manual adapter). Reputation is signed credentials, not stake. | Avoids the regulatory and morale tax of token economies, keeps the project usable in jurisdictions that treat tokens as securities. |
| **P4** | **Signatures, not trust assertions.** Every state-changing claim — group create, vote, message, mandate, achievement — carries an Ed25519 signature over canonical JSON. Servers verify; they don't gatekeep. | Makes any node replaceable. A compromised server can't forge history; a forgotten one can be re-synced from any other holder of the signed records. |

---

## 1 — Where we are (v0.9.8, June 2026)

Snapshot of shipped capabilities, with the maturity flag we'll use to
plan from.

| Capability | Module | Maturity | Notes |
|-----------|--------|----------|-------|
| Agent identity (Ed25519 + did:key) | `nth_dao.identity`, `nth_dao.did_key` | 🟢 stable | Signing path, verify path, recovery kit |
| Per-agent contribution log | `nth_dao.agent_ledger` | 🟢 stable | Hash-chained, signed, append-only |
| Achievement credential (monthly fold) | `nth_dao.achievement` | 🟢 stable | W3C VC 2.0, Ed25519Signature2020 |
| Team-level signed event stream | `nth_dao.event_bus` | 🟡 v0.9.7 | Hash chain landed; needs the AgentLedger→EventBus bridge |
| Channel / message / task / announcement | `nth_dao.groups` | 🟢 stable | GroupManager, audit events, kind+reply_to |
| DAO registry (unique names + governance) | `nth_dao.group_registry` | 🟢 stable | Cross-workspace slugs, policy votes |
| Multi-DAO sidebar + per-DAO scope | `nth_dao.web`, `frontend/src/App.tsx` | 🟢 v0.9.7 | Home + group DAOs, scoped channels |
| LAN discovery (UDP + mDNS) | `nth_dao.discovery.lan*` | 🟢 stable | mDNS via `[lan]` extra |
| Guardian-based key recovery | `nth_dao.guardian` | 🟢 stable | N-of-M threshold signature |
| Browser Ed25519 wallet | `frontend/src/crypto.ts` | 🟢 v0.9.6 | Non-extractable CryptoKey in IndexedDB |
| Visible agent codes + echo responder | `nth_dao.agent_code`, `demo_responder` | 🟢 v0.9.8 | Closes "agent doesn't reply" UX gap |
| **Settlement (Mandates + adapters)** | — | ⚫ not built | The next chapter |
| **A2A inbound/outbound adapter** | — | ⚫ not built | Required for cross-vendor agent interop |
| **On-chain anchor (ERC-8004)** | — | ⚫ not built | Optional reputation bridge |
| **Multi-DAO mesh (cross-DAO routing)** | — | ⚫ not built | Beyond the local sidebar |
| **Autonomous agent runtime** | — | ⚫ not built | Required for "self-running DAOs" |

Tests: **336 passing / 7 skipped / 0 failed**. Build clean. Repo healthy.

---

## 2 — The three horizons

### Short term: **v0.10 → v0.13** (now → 3 months, **Sept 2026**)
**Theme**: *Open the perimeter.* Make NTH DAO speak the open agent web.

Deliverables:
- Mandate primitives (IntentMandate / CartMandate / PaymentMandate)
- A2A v1.0 server + client + Agent Card
- x402 settlement adapter (USDC via Coinbase)
- ERC-8004 reputation publish (write-only, no chain reads)

End state: an NTH DAO member can pay an external A2A agent in USDC,
get back a signed receipt, anchor reputation outcomes on Ethereum.

### Medium term: **v0.14 → v1.5** (3 → 12 months, **June 2027**)
**Theme**: *Produce, don't prototype.* Hardening, marketplace, mesh.

Deliverables:
- Multi-DAO mesh (route an A2A task across federated DAOs)
- Mission marketplace (open bidding on Mandate-priced missions)
- Agent payroll automation (recurring PaymentMandate from DAO treasury)
- AGNTCY manifest publisher
- Mobile companion app (TS / Capacitor)
- Production observability: traces, metrics, SLOs
- Full conformance test suite (cross-implementation validation)
- v1.0 ABI freeze

End state: NTH DAO can host a 50-agent DAO running 24/7 with
self-funding mission queue, federated discovery, and observability
that an SRE would call "production-grade."

### Long term: **v2.0 → v3.0** (12 → 36 months, **June 2029**)
**Theme**: *The autonomous DAO operating system.*

Deliverables:
- Autonomous agent runtime (agents trigger their own work without
  human poll cycles)
- Cross-DAO settlement (Mandate routing across DAO meshes)
- DAO-to-DAO contracts (multi-party signed agreements with on-chain
  escrow option)
- Privacy-preserving agent computation (TEE / FHE plugins for
  payload secrecy)
- Federated reputation graph (read-only ERC-8004 + IPFS aggregation)
- Constitutional governance (canonical bylaw documents + amendment
  voting, all signed)

End state: NTH DAO is what AGNTCY's "Internet of Agents" mission
statement actually describes — except open-source, local-first, and
without a token tax.

---

## 3 — Architecture, pillar × horizon

Six pillars. Each pillar gets one paragraph per horizon. The grid
makes investment trade-offs explicit.

### Pillar A — Identity & Trust

```
Layer 4: Federated reputation graph                       [v2.0+]
Layer 3: ERC-8004 anchor + DID resolution                 [v0.13]
Layer 2: AchievementCredential + AgentLedger              [shipped]
Layer 1: did:key + W3C VC + Ed25519Signature2020          [shipped]
Layer 0: AgentIdentity (Ed25519 keypair)                  [shipped]
```

**Short** (v0.10–v0.13): Add `nth_dao.bridge.erc8004` —
publish-only function that takes any signed credential and writes
its digest + issuer DID to an ERC-8004 reputation registry on Base
or Optimism. Reads stay local. Default RPC endpoint configurable.

**Medium** (v0.14–v1.5): Add a *read* path that ingests external
ERC-8004 attestations into our local trust graph (`nth_dao.web_of_trust`),
weighted by signer reputation. Adds a DID resolver chain (`did:key`,
`did:web`, `did:ethr` — first two are pure crypto, last is on-chain).

**Long** (v2.0+): Federated graph. Each NTH DAO instance publishes its
trust graph as a signed IPFS bundle; instances subscribe to bundles
from other DAOs they trust. Cross-DAO reputation queries become a
graph-walk over signed bundles, no chain required.

### Pillar B — Discovery & Routing

```
Layer 4: DAO mesh routing (cross-DAO Tasks)               [v0.14]
Layer 3: AGNTCY manifest + A2A Agent Card                 [v0.11/v0.13]
Layer 2: GroupRegistry (cross-workspace slugs)            [shipped]
Layer 1: PeerFinder + AgentRegistry                       [shipped]
Layer 0: LAN UDP + mDNS                                    [shipped]
```

**Short** (v0.11): `nth_dao.a2a.agent_card` generates
`/.well-known/agent.json` from local capabilities. `nth_dao.a2a.server`
exposes 4 core A2A RPC methods over JSON-RPC. `nth_dao.a2a.client`
calls outbound. AGNTCY discovery shape published at v0.13 alongside
ERC-8004 anchor.

**Medium** (v0.14): Multi-DAO mesh. When an inbound A2A task asks for
a capability not present in this DAO, the router forwards it to a
configured peer DAO (over A2A) that does have it, signs an attestation,
returns the result. Routing tables are themselves DAO-governed.

**Long** (v2.0+): Reputation-weighted routing. The mesh picks the next
hop by a cost function over (latency, reputation score, declared
settlement rates). Failed hops penalise the reputation registry.

### Pillar C — Collaboration (Channels / Missions / Blackboard)

```
Layer 4: Cross-DAO mission federation                     [v2.0+]
Layer 3: Marketplace + bidding                            [v1.0]
Layer 2: A2A Task ↔ Mission bridge                        [v0.11]
Layer 1: Mission + Blackboard + Channel + Task            [shipped]
Layer 0: Signed events on EventBus                        [shipped]
```

**Short** (v0.11): Incoming A2A `SendMessage` lands as a Mission step
on the local Blackboard. Outgoing Mission steps that need an external
agent are sent as A2A `SendMessage` to peer Agent Cards. Task lifecycle
(`CreateTaskPushNotificationConfig`) wired into Mission state machine.

**Medium** (v1.0): Marketplace. Missions can be posted with an
attached IntentMandate (max-budget / deadline / acceptance criteria).
Agents bid via CartMandate. The DAO accepts via PaymentMandate. The
queue is on EventBus, fully replayable.

**Long** (v2.0+): Federation. A mission in DAO-A can declare a step
that must be executed in DAO-B (because B has the privileged
capability). The mesh routes, signs proofs at each hop, and settles
via cross-DAO PaymentMandate.

### Pillar D — Settlement (the encrypted transaction interface)

```
Layer 4: Cross-DAO escrow + multi-party Mandates          [v2.0+]
Layer 3: ERC-8004/IPFS anchor + AGNTCY visibility         [v0.13]
Layer 2: Settlement adapters (x402, AP2 card, manual)     [v0.12]
Layer 1: Mandate primitives (Intent / Cart / Payment)     [v0.10]
Layer 0: Existing Ed25519 + W3C VC + canonical JSON       [shipped]
```

**Short** (v0.10–v0.12): Three new modules.

- `nth_dao.mandate` — three dataclasses + sign/verify helpers, mirrors
  AP2 exactly. ~400 LOC.
- `nth_dao.settle.base.SettlementAdapter` ABC: `present(cart)` →
  `settle(payment)` → `receipt`. Adapters registered at bootstrap.
- `nth_dao.settle.x402` — Coinbase HTTP-402 client. USDC default.
- `nth_dao.settle.manual` — log + EventBus emit only, for DAOs that
  settle out-of-band.

UI: a "Approve payment" pane in the web console signs PaymentMandate
with the existing browser wallet (`frontend/src/crypto.ts`).

**Medium** (v1.0): Marketplace integration — Mission posting includes
an IntentMandate; matching engine produces CartMandates; acceptance
generates PaymentMandate; settlement adapter runs; receipt becomes a
new credential type (`ServiceProvenanceCredential`).

**Long** (v2.0+): Multi-party Mandates. A Cart that needs three agents
to collaborate produces three Payments that all reference the same
Intent. Optional on-chain escrow contract holds funds until all three
deliveries are attested.

### Pillar E — Governance

```
Layer 4: Constitutional bylaws + amendment voting         [v2.0+]
Layer 3: Delegated voting + quadratic vote weights        [v1.0]
Layer 2: Policy votes (OPEN/APPROVAL/CLOSED/VOTED)        [shipped]
Layer 1: GroupRegistry membership + admin set             [shipped]
Layer 0: Founder signatures + Guardian recovery           [shipped]
```

**Short** (v0.10–v0.13): No change. Governance already handles policy
change, member admit/expel via vote. Just add EventBus emit on every
governance state transition so external observers can audit.

**Medium** (v1.0): Delegated voting. A member can delegate their vote
to another member (signed, revocable). Quadratic weighting optional
per DAO. UI: "Delegate to" picker.

**Long** (v2.0+): Constitutional bylaws. A DAO publishes a signed
canonical bylaw document. Amendments require a supermajority vote
under the current bylaws. Bylaws can reference Mandate templates, so
"any payment >$10k requires 2/3 admin signatures" becomes a
machine-enforced rule rather than a wiki note.

### Pillar F — Observability & Operations

```
Layer 4: Federated SLO dashboards                         [v2.0+]
Layer 3: Conformance vectors + cross-impl validation      [v1.0]
Layer 2: Traces + metrics + structured logs               [v0.14]
Layer 1: EventBus replay + verify_chain()                 [shipped]
Layer 0: nth-status / nth-metrics CLI                     [shipped]
```

**Short** (v0.10–v0.13): No new investment beyond `nth-status` /
`nth-metrics` polish. EventBus is already the audit substrate.

**Medium** (v0.14): OpenTelemetry traces. Every signed event becomes a
trace span. Metrics: per-DAO message rate, mission throughput,
settlement volume, signature verification ratio, peer count.
Dashboards run on Grafana or any TSDB-compatible system.

**Long** (v2.0+): Federated dashboards. DAOs voluntarily publish
anonymised metrics into a federated aggregator (run by trusted
infrastructure DAOs). The aggregator gives the ecosystem visibility
without revealing tenant data.

---

## 4 — Decision gates

Between horizons, four gates we *must* pass. Each gate has a binary
question and a concrete metric. Missing the gate means the next
horizon doesn't start.

### Gate G1: v0.13 → v0.14 ("open perimeter is real")
- ✅ At least one external A2A agent has been paid by an NTH DAO via x402
- ✅ At least one reputation credential anchored to ERC-8004 with valid
      on-chain digest
- ✅ A2A inbound conformance: passes 80%+ of Google's reference vectors
- ✅ No P1 security issues open against `nth_dao.mandate` or
      `nth_dao.bridge.erc8004`

### Gate G2: v1.0 → v1.1 ("production ABI freeze")
- ✅ 500+ messages/min sustained on a single workspace with verify_chain
      still green
- ✅ Cross-implementation conformance: at least one third-party
      implementation (Rust, Go, or TS) passes all vectors
- ✅ No public schema break in the previous 3 months
- ✅ SECURITY.md threat model audited by an external party

### Gate G3: v1.5 → v2.0 ("federation works")
- ✅ Three independent NTH DAO instances complete a cross-DAO mission
      with mesh routing + cross-DAO settlement
- ✅ Federated reputation graph: at least 1000 signed credentials in
      circulation across instances
- ✅ Production incident MTTR < 4 hours for 90 days

### Gate G4: v2.x → v3.0 ("autonomous DAOs sustain themselves")
- ✅ At least one DAO has run for 90 days with no human admin
      intervention beyond the original guardian set
- ✅ At least one constitutional amendment proposed and ratified under
      a DAO's own bylaws

---

## 5 — Risk register

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|------------|--------|------------|
| **R1** | A2A spec breaks v1.x → v2.x and we lag | Medium | High | Track spec, ship adapter behind versioned namespace `nth_dao.a2a.v1` from day one |
| **R2** | x402 / ERC-8004 ecosystem fork; choose wrong fork | Medium | Medium | SettlementAdapter ABC + multi-bridge means swapping is cheap |
| **R3** | Local-first stance limits adoption to hobbyists | Medium | Critical | Mid-term marketplace + payroll are the commercial hook; without them we stay a tools project |
| **R4** | Settlement layer attracts regulatory scrutiny | Medium | High | We never hold funds; Mandates are user-signed; settlement runs through external facilitators |
| **R5** | Browser wallet UX too friction-heavy → users skip signing | High | High | v0.12 ships `auto_approve_max` per DAO; sub-threshold payments self-sign within Intent constraints |
| **R6** | Federation introduces Sybil attacks via fake DAOs | Medium | High | Reputation weighting; cross-DAO endorsements; mesh routing penalises low-reputation hops |
| **R7** | Maintainer burnout (small team, broad surface) | High | Critical | Hard scope discipline; reject any v0.x feature that doesn't have a Gate metric attached |
| **R8** | "Local-first" interpreted as "no internet at all" → users disable LAN discovery for security | Low | Medium | Documentation + opt-in flags; UDP discovery already has PSK option for private subnets |

---

## 6 — Module map (full target state at v1.5)

```
nth_dao/
├── identity.py            ✓ Ed25519, did:key bridge
├── did_key.py             ✓ W3C did:key encoder/decoder
├── agent_code.py          ✓ visible Telegram-style handles
├── agent_ledger.py        ✓ per-agent hash-chained log
├── achievement.py         ✓ AchievementCredential (W3C VC)
├── event_bus.py           ✓ team-level signed event stream
├── groups.py              ✓ channels / messages / tasks / announcements
├── group_registry.py      ✓ unique DAO names + governance votes
├── guardian.py            ✓ N-of-M social recovery
├── membership.py          ✓ JoinPolicy + roles
├── reputation.py          ✓ trust hints + WoT primitives
├── web_of_trust.py        ✓ subjective trust graph
├── invitation.py          ✓ signed invitations
├── key_recovery.py        ✓ passphrase-protected kits
├── marketplace.py         ✓ mission posting + matching (v1.0 hardened)
├── demo_responder.py      ✓ in-process echo agent
├── discovery/
│   ├── agent_registry.py  ✓ workspace registry
│   ├── peer_finder.py     ✓ search + ranking
│   ├── lan.py             ✓ UDP broadcast
│   └── lan_mdns.py        ✓ mDNS via [lan] extra
├── orchestration/
│   ├── mission.py         ✓ step DAG
│   ├── mission_store.py   ✓ persistence
│   ├── runner.py          ✓ execution
│   └── marketplace_bridge.py     ● [v1.0] Mandate ↔ Mission
├── mandate/               ● [v0.10] AP2-shape Mandates
│   ├── __init__.py
│   ├── intent.py
│   ├── cart.py
│   ├── payment.py
│   └── service_provenance.py     # post-settlement credential
├── settle/                ● [v0.12] adapters
│   ├── base.py            # SettlementAdapter ABC
│   ├── x402.py            # Coinbase HTTP-402 / USDC
│   ├── ap2_card.py        # AP2 card-rail facilitator
│   └── manual.py
├── a2a/                   ● [v0.11] A2A adapter
│   ├── agent_card.py      # /.well-known/agent.json generator
│   ├── server.py          # JSON-RPC 2.0 server (4 core methods)
│   ├── client.py          # outbound RPC client
│   └── task_bridge.py     # A2A Task ↔ Mission
├── bridge/                ● [v0.13] write-only outbound
│   ├── erc8004.py         # Ethereum reputation registry
│   ├── ipfs.py            # content-address credentials
│   └── agntcy.py          # AGNTCY manifest publisher
├── mesh/                  ● [v0.14] multi-DAO routing
│   ├── router.py          # forward unhandled tasks to peer DAOs
│   └── trust_table.py     # which peers to trust for which capabilities
├── conformance/           ✓ + [v1.0] expanded
│   └── vectors.json       # canonical test vectors
├── web/                   ✓ FastAPI + React TS console
├── cli/                   ✓ nth-status, nth-metrics
└── util/                  ✓ atomic_write_json, InterProcessLock, etc.
```

`frontend/src/` mirrors above for any UI-touching capability.

---

## 7 — Sprint zero (immediate next 4 weeks)

These are the v0.10 tickets, sized + sequenced. Each one ≤ 5 days of
work and produces a green-test PR that doesn't break v0.9.8.

### Week 1 — Mandate primitives

- **T-1**: `nth_dao/mandate/intent.py` — `IntentMandate` dataclass +
  `build / sign / verify`. Mirrors `AchievementCredential` shape.
  Tests: 12.
- **T-2**: `nth_dao/mandate/cart.py` — `CartMandate` + binding to
  intent digest. Tests: 10.
- **T-3**: `nth_dao/mandate/payment.py` — `PaymentMandate` + binding to
  cart digest. Tests: 10.

### Week 2 — Conformance + EventBus types

- **T-4**: Add 6 canonical Mandate test vectors to
  `nth_dao/conformance/vectors.json` (3 valid, 3 invalid: bad sig,
  bad binding, expired).
- **T-5**: Reserve event types in `nth_dao/event_bus.py` documentation:
  `mandate.intent.issued`, `mandate.cart.received`,
  `mandate.payment.authorised`, `settlement.completed`.
- **T-6**: Facade re-export. Add `IntentMandate / CartMandate /
  PaymentMandate / build_* / sign_* / verify_*` to `nth_dao/__init__.py`.

### Week 3 — A2A skeleton (early start)

- **T-7**: `nth_dao/a2a/agent_card.py` — generator that turns the
  current agent's `capabilities` list into an A2A-compliant JSON.
- **T-8**: `nth_dao/a2a/server.py` — bare JSON-RPC server with one
  method (`GetTask`) implemented; the rest return 501. Routed off
  `/.well-known/agent.json` + `/a2a/jsonrpc`.

### Week 4 — UI + release

- **T-9**: Web console "Mandate" sidebar — list issued intents,
  pending carts, completed payments, with verify button per row.
- **T-10**: CHANGELOG + version bump to `0.10.0`; tag; push;
  conformance vectors validated against Google's A2A reference suite.

**Definition of done for v0.10**: 30+ new tests; nothing in v0.9.8
regressed; the maintainer can sign an `IntentMandate` from the web
console and `verify_credential()` accepts it.

---

## 8 — Strategic posture (for the public-facing story)

Three sentences NTH DAO is allowed to put on its homepage at each
horizon:

- **v0.13 (Sept 2026)**: "An open-source, local-first DAO collaboration
  layer where AI agents can find each other on a LAN, work together
  on Missions, settle in USDC, and anchor reputation on-chain — all
  with their own Ed25519 keys, no central server, no token."

- **v1.0 (June 2027)**: "Production-grade infrastructure for multi-agent
  DAOs. Marketplace-priced missions, federated discovery, ABI-frozen
  protocol, three third-party implementations. Run on a laptop or a
  cluster."

- **v3.0 (June 2029)**: "The autonomous DAO operating system. Run a
  10-agent organisation for a year with no human intervention beyond
  the constitutional founders. AGNTCY's Internet of Agents promise,
  delivered without a token tax."

---

## 9 — What we explicitly do NOT build

These deserve documentation as much as what we *do* build, because
they're the most-asked rejections.

| Item | Why not |
|------|---------|
| **A native NTH token** | Adds regulatory risk, distracts from utility, breaks the "no token tax" promise. Settlement uses external rails. |
| **A KYC service** | Skyfire KYA exists for that. We carry KYA attestations when a DAO uses them, but we don't run KYC ourselves. |
| **An on-chain bridge in the read direction (default)** | The local-first stance means we don't *require* internet. Reads are opt-in, never the default. |
| **A central directory of DAOs** | GroupRegistry is workspace-local. AGNTCY can be the directory if a DAO opts in. Otherwise DAOs discover via LAN, A2A, or peer-sharing. |
| **A managed-service / hosted-NTH-DAO offering** | Out of scope for the open-source project. Commercial vendors are welcome to build it on top. |
| **Replacing LangGraph / CrewAI / AutoGen** | We bridge to them. They orchestrate one agent's work; we coordinate many agents across DAOs. Different altitude. |

---

## 10 — Open questions for the maintainers (resolve before v0.10 closes)

1. **Mandate signing UX**: per-payment wallet prompt (safe-by-default) or
   `auto_approve_max` per DAO (industry-aligned for sub-threshold
   amounts)?
   - **Recommendation**: ship `auto_approve_max = 0` default in v0.10;
     add UI to raise it in v0.12 when adapters land.

2. **First-class settlement chain**: Base, Optimism, Polygon, or
   Ethereum mainnet for the v0.13 ERC-8004 anchor?
   - **Recommendation**: Base. Lowest fees, x402's anchor chain,
     fastest finality among rollups.

3. **Conformance vector authority**: do we accept Google's A2A reference
   vectors verbatim, or fork them for NTH DAO?
   - **Recommendation**: accept verbatim. Forking creates "compatible
     except…" footnotes that kill adoption.

4. **AGNTCY membership**: do we apply to be a maintainer of the AGNTCY
   project at Linux Foundation, or stay a downstream consumer?
   - **Recommendation**: downstream consumer until v1.0 ships. Becoming
     a maintainer dilutes focus from our own roadmap.

5. **Mobile companion**: native (React Native) or Capacitor wrap of
   the existing web console?
   - **Recommendation**: Capacitor in v1.0; revisit native if metrics
     show > 20% DAU on mobile by v1.5.

---

## 11 — Closing read

The path from where we are (June 2026, v0.9.8, 336 tests passing) to
where we want to be (June 2029, autonomous DAOs running themselves) is
not technologically the hard part. The hard part is **discipline**:

- Refusing to add a token even when "tokenomics" is what every Twitter
  thread asks for.
- Refusing to make on-chain mandatory even when "DePIN" is the funding
  pitch that opens VC doors.
- Refusing to centralise even one byte of state even when "managed
  service" would be cash-positive in a week.

Every horizon in this document earns its right to exist by passing a
Gate metric. Every pillar stays within the four operating principles.
Every module on the target map is opt-in, replaceable, and signed.

If we stick to that, the v3.0 promise is small. We just keep landing
the v0.10 sprint, and the v0.11 after that, and the v0.12 after that.
The architecture follows from the principles.

Good roadmaps make the next four weeks obvious. The next four weeks
are in §7. Start there.
