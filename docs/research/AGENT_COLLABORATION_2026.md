# Agent Collaboration 2026 — Landscape + NTH DAO Design Update

**Status**: design proposal, supersedes the 2026-Q1 view in `A2A_ALIGNMENT.md`
**As of**: June 2026
**Audience**: NTH DAO maintainers + integrators
**Decision target**: where to invest the next 3 release cycles (v0.10 → v0.13)

---

## TL;DR

Three new things landed since `A2A_ALIGNMENT.md` was written and they
collectively change the design tradeoff:

1. **A2A reached v1.0** in early 2026. It's no longer a draft — it's the
   protocol the operator ecosystem is wiring against. We need a real
   bridge, not a "we're conceptually compatible" footnote.

2. **The payment / settlement layer crystallized.** AP2 + x402 + ERC-8004
   together form a coherent "encrypted transaction interface" with
   verifiable mandates, stablecoin micropayment rails, and optional
   on-chain anchoring. None of these existed at production scale a year
   ago. All three are vendor-neutral.

3. **Identity converged on did:key + W3C VC + on-chain anchor.** NTH DAO
   already does the first two; ERC-8004 makes the third optional but
   real. The user's brief — *"留有加密交易接口/底座"* — is the same idea.

This doc proposes adding an **NTH DAO settlement layer** that exposes
AP2-style Mandate primitives, hooks for x402 settlement, and an optional
ERC-8004 anchor — without giving up the local-first, no-chain-required
default that makes NTH DAO usable in a private LAN with zero crypto rails.

---

## Part 1 — Landscape, June 2026

### Stack 1: Protocol — how agents talk to each other

| Protocol | Sponsor | What it is | Maturity |
|----------|---------|------------|----------|
| **A2A v1.0** | Google → Linux Foundation | JSON-RPC over HTTP(S) + SSE; Agent Cards at `/.well-known/agent.json`; 11 RPC methods | **Production**, 150+ orgs |
| **AGNTCY** | Cisco / LangChain / LlamaIndex / Galileo / Glean → Linux Foundation | "Internet of Agents" — discovery + identity + messaging + observability layered stack | Active, broader scope than A2A |
| **MCP** | Anthropic | Tool layer (model ↔ external systems), not agent ↔ agent | Stable, ecosystem-wide |
| **AP2 (Agent Payments Protocol)** | Google + payments cos. | A2A *extension* for payments. Carries three W3C VC "Mandates": **Intent**, **Cart**, **Payment** | Production-grade (2026) |

**Read**: A2A is the wire; AGNTCY is the broader org-level stack; MCP is
orthogonal (it's how a single agent reaches tools). NTH DAO is at the
same altitude as A2A but with a DAO-shaped lens instead of an enterprise
RPC lens.

### Stack 2: Settlement — how agents pay each other

| Standard | Sponsor | Layer | What it adds |
|----------|---------|-------|--------------|
| **x402** | Coinbase | HTTP semantic | Reuses HTTP 402 "Payment Required" for stablecoin micropayments. V2 (Dec 2025) is multi-chain. 100M+ payments in first six months. |
| **ERC-8004** | Ethereum (EIP draft, 2025–2026) | On-chain identity + reputation | "Passport for the Agentic Web" — NFT-based portable agent ID, reputation registry, slashable reputation stake |
| **ERC-8183** | Virtuals + Ethereum dAI team | On-chain transaction framework | Trustless permissionless framework for agent-to-agent transactions; co-developed March 2026 |
| **Skyfire KYAPay** | Skyfire | Settlement + KYA | USDC instant settlement + Know-Your-Agent compliance layer. Records KYA IDs as ERC-8004 attributes. |
| **Mastercard Verifiable Intent** | Mastercard + Google | Trust layer | Tokenized spending mandates carried with transactions; protocol-agnostic; March 2026 |
| **Visa Trusted Agent Protocol** | Visa | Trust layer | Visa's parallel; same idea, different network |

**Read**: The settlement stack is now a layered cake:
- **Intent layer**: signed Mandates (AP2 / Verifiable Intent / VC-shaped)
- **Wire layer**: HTTP-402 (x402) — works with stablecoins or fiat rails
- **Identity layer**: ERC-8004 anchor (optional, for portability across networks)

These are deliberately decoupled. You can do Mandates without x402. You
can do x402 without ERC-8004. NTH DAO will offer the bottom three as
opt-in layers, with Mandates being mandatory for any payment flow.

### Stack 3: Orchestration — frameworks that build agents

| Framework | Best for | Production readiness |
|-----------|----------|---------------------|
| **LangGraph** | Stateful graphs, checkpointing, streaming | ⭐⭐⭐⭐⭐ — battle-tested, LangSmith observability |
| **CrewAI** | Role-based teams, fast prototyping | ⭐⭐⭐⭐ — lowest barrier to entry |
| **AutoGen / AG2** | Event-driven conversational | ⭐⭐⭐⭐ — async-first rewrite shipped |
| **Letta (formerly MemGPT)** | Persistent memory assistants | ⭐⭐⭐ — specialized |
| **Olas** | User-owned autonomous services + token incentives | ⭐⭐⭐⭐ — DeFi-native, OLAS token rails |
| **Fetch.ai** | Marketplace of agents + AEA framework | ⭐⭐⭐ — established, AGIX merger context |
| **Virtuals Protocol** | Tokenized agents + ERC-8183 | ⭐⭐⭐ — Base ecosystem, growing |

NTH DAO is **not** in this column — we're protocol-and-DAO-shape, agent
backends bring their own framework. We bridge them.

### Stack 4: Reputation / Identity — who is this agent

| Standard | Used by | Notes |
|----------|---------|-------|
| **did:key** | NTH DAO (today), most VC stacks | Self-sovereign, no registry needed |
| **W3C Verifiable Credentials** | AP2 Mandates, AGNTCY identity, NTH DAO AchievementCredential | The shared serialization for "X attested to Y" |
| **ERC-8004 reputation registry** | Skyfire, Virtuals, on-chain DeFi-agent ecosystem | Portable across networks, slashable stake |
| **KYA (Know Your Agent)** | Skyfire | Compliance overlay on top of ERC-8004 |
| **NTH AgentLedger** | NTH DAO (today) | Per-agent append-only hash-chained contribution log |
| **NTH AchievementCredential** | NTH DAO (today) | Monthly fold of AgentLedger as signed VC |

**Read**: NTH DAO already speaks did:key + W3C VC; the missing piece is
*portability* across networks. ERC-8004 anchor is the bridge.

---

## Part 2 — NTH DAO's Stance (Updated)

The first principle stays the same: **local-first, decentralized,
DAO-shaped collaboration that runs on a private LAN with zero blockchain
dependency, but with optional bridges out to the open agent web.**

What's new in this update:

1. **A2A as the export protocol, not the substrate.** NTH DAO's internal
   transport stays the way it is (FastAPI + Ed25519 + GroupRegistry +
   EventBus). When a member needs to interact with an outside agent that
   speaks A2A, we expose `nth_dao.a2a` as a thin adapter. We do NOT
   replace our internal model with A2A — A2A doesn't have DAO membership,
   policy votes, contribution ledgers, or hash-chained team audits. Our
   model is richer for the use case we care about.

2. **Settlement is opt-in, not bundled.** A DAO can run NTH DAO without
   any payment rail. When a DAO wants to settle work (e.g. paying an
   external agent for a code review), the workflow is:

   ```
   DAO ─── signs IntentMandate ───▶ counterparty
       ◀── signs CartMandate ──── counterparty
   DAO ─── signs PaymentMandate ─▶ x402 endpoint  (or ACH/card via AP2)
       ◀── settlement receipt ──── x402 / AP2 facilitator
   DAO ─── EventBus.emit(...) ───▶ on-chain anchor (optional ERC-8004 receipt)
   ```

   All Mandates are W3C VCs signed with the DAO's Ed25519 key, so any
   external verifier — AP2 facilitator, x402 gateway, ERC-8004 indexer —
   can validate them without trusting our store.

3. **Reputation is local-first AND exportable.** AchievementCredential
   (v0.9.6) stays the canonical record of "what this agent did this
   month." The new addition is an export path: a one-shot
   `nth_dao.bridge.erc8004.publish(credential)` that anchors the
   credential's digest on-chain so external DeFi-native agents can read
   it. **Never** does NTH DAO require the anchor — it's purely an
   outgoing bridge.

4. **AGNTCY discovery for cross-DAO surfaces.** Our GroupRegistry is the
   inside view. For external discovery (one DAO finding another DAO's
   agents on the open agent web), we publish an AGNTCY-shaped manifest.
   We don't operate AGNTCY infrastructure; we just speak its dialect.

---

## Part 3 — The Encrypted Transaction Interface (the new layer)

This is the user's brief — *"留有加密交易接口/底座"*. Below is the
concrete shape.

### Layer 0: Mandate primitives (mandatory if you opt in)

A new module `nth_dao/mandate.py` introduces three W3C VC types that
mirror AP2's structure exactly:

```python
@dataclass
class IntentMandate:
    """The DAO authorises an agent to act within parameters."""
    issuer_did: str            # did:key of the authoriser (DAO or human)
    agent_did: str             # did:key of the acting agent
    purpose: str               # "buy code review", "pay for compute"
    constraints: dict          # max_amount, currency, expiry, allowed_counterparties
    expires_at: str            # ISO-8601
    proof: Ed25519Signature2020

@dataclass
class CartMandate:
    """The counterparty's offer for a specific transaction."""
    issuer_did: str            # the counterparty
    intent_mandate_digest: str # binds to the IntentMandate
    items: list                # what's on offer
    total: Money               # amount + currency
    settlement_methods: list   # ["x402:usdc", "ap2:card", "ach:..."]
    expires_at: str
    proof: Ed25519Signature2020

@dataclass
class PaymentMandate:
    """The DAO accepts the cart and authorises settlement."""
    issuer_did: str            # DAO
    cart_mandate_digest: str   # binds to the CartMandate
    settlement_choice: str     # which of the cart's settlement_methods
    proof: Ed25519Signature2020
```

All three are **W3C VC 2.0 shape**, **Ed25519Signature2020**, and use
`canonical_json` for the proof input — identical to the existing
`AchievementCredential` machinery. This means our existing crypto code
covers them at zero marginal infra cost.

### Layer 1: Settlement adapters (opt-in)

```
nth_dao/settle/
├── __init__.py
├── base.py        # SettlementAdapter ABC: present(cart) → settle(payment) → receipt
├── x402.py        # Coinbase x402 client; settles in USDC via HTTP 402
├── ap2_card.py    # AP2 facilitator for traditional card rails
└── manual.py      # "log it and move on" — for DAOs that settle out-of-band
```

The bot / web console never imports `x402` or `ap2_card` directly — it
imports `SettlementAdapter` and the configured adapter is registered at
DAO bootstrap. Same shape as our existing backend registry.

### Layer 2: On-chain anchor (opt-in, write-only)

```
nth_dao/bridge/
├── __init__.py
├── erc8004.py     # publish(credential) → tx_hash; record digest + tx_hash
│                  # to EventBus as "credential.anchored"
└── ipfs.py        # publish(credential) → CID; same EventBus shape
```

The bridge is **write-only** from NTH DAO's perspective. We never read
chain state for the local-first paths. External verifiers read the chain
themselves.

### Layer 3: Audit (built on existing EventBus)

The team-level EventBus (`nth_dao.event_bus`, landed v0.9.7) already
hash-chains signed events. Settlement adds four event types:

- `mandate.intent.issued`
- `mandate.cart.received`
- `mandate.payment.authorised`
- `settlement.completed` (payload includes adapter id + receipt digest)

Anyone replaying the EventBus can reconstruct every commercial
interaction the DAO entered, verify the signatures, and (if the
on-chain anchor is enabled) cross-check against the chain. This is the
"加密交易底座" the user asked for, made concrete.

### What we do NOT build

- A token. NTH DAO has zero token ambitions. Settlement uses external
  rails (USDC via x402, fiat via AP2, anything via the manual adapter).
- A reputation oracle. ERC-8004 is the standard; we publish into it via
  the bridge, we don't run the registry.
- A KYA service. Skyfire owns KYA; we record their attestation as a VC
  in AgentLedger if a DAO uses Skyfire.
- A facilitator. x402 and AP2 both need facilitators (settlement
  gateways). We talk to them, we don't be one.

---

## Part 4 — Mapping to current NTH DAO code

| Capability | Today (v0.9.7) | After v0.13 | Net delta |
|-----------|---------------|-------------|-----------|
| Agent identity | Ed25519 + did:key (`nth_dao.identity`) | Same + optional ERC-8004 anchor | One new module: `bridge/erc8004.py` |
| Discovery (LAN) | `LANDiscovery` (UDP) + `MDNSDiscovery` (mDNS) | Same + optional A2A Agent Card publisher | New module: `a2a/agent_card.py` |
| Discovery (cross-DAO) | `GroupRegistry` (local) | Same + optional AGNTCY manifest | New module: `agntcy/manifest.py` |
| Team audit log | `EventBus` (v0.9.7, hash-chained signed) | Same + four new event types | No code, just type vocabulary |
| Per-agent ledger | `AgentLedger` | Same | No change |
| Reputation export | `AchievementCredential` (v0.9.6) | Same + ERC-8004 publish path | Uses the new bridge |
| Settlement | None | `IntentMandate` / `CartMandate` / `PaymentMandate` + adapters | New module: `mandate.py` + `settle/` |
| External wire | None | A2A adapter (in + out) | New module: `a2a/server.py` + `a2a/client.py` |
| Web console | FastAPI + React + Ed25519 wallet | Same + Mandate signing in the wallet | UI: a "Approve transaction" pane |

**Net additions: 7 new modules, ~2000 LOC, all opt-in.**
**Net subtractions: zero — nothing in the current codebase needs to change.**

---

## Part 5 — Roadmap (v0.10 → v0.13)

### v0.10 — Mandate primitives + EventBus types
**Goal**: Have the data model in place; nothing settles yet.

- `nth_dao/mandate.py` — IntentMandate / CartMandate / PaymentMandate dataclasses
- W3C VC 2.0 serialization, Ed25519Signature2020 proofs
- `build / sign / verify` lifecycle, mirroring `AchievementCredential`
- New EventBus event types reserved in `nth_dao/event_bus.py`
- Conformance vectors for Mandate sign/verify (cross-impl validation)
- Tests: ~30

### v0.11 — A2A adapter (in + out)
**Goal**: NTH DAO members can send tasks to external A2A agents and accept tasks from them.

- `nth_dao/a2a/agent_card.py` — generate `/.well-known/agent.json`
  from the local agent's capabilities
- `nth_dao/a2a/server.py` — JSON-RPC server exposing 4 core methods
  (`SendMessage`, `GetTask`, `CreateTaskPushNotificationConfig`, list)
- `nth_dao/a2a/client.py` — JSON-RPC client for outbound calls
- Bridge to GroupManager: an incoming A2A task lands as a Mission step
- Tests: A2A wire-compat conformance using Google's reference vectors

### v0.12 — Settlement adapters
**Goal**: A DAO can pay an external agent in USDC via x402 or via the manual fallback.

- `nth_dao/settle/base.py` — SettlementAdapter ABC
- `nth_dao/settle/x402.py` — Coinbase x402 client; HTTP 402 + USDC
- `nth_dao/settle/manual.py` — for DAOs that settle out-of-band
- `nth_dao/settle/ap2_card.py` — stretch: card-rail AP2 facilitator (likely v0.13)
- Web console: "Approve payment" pane that signs `PaymentMandate` with
  the browser-resident Ed25519 wallet (already in `frontend/src/crypto.ts`)
- Tests: end-to-end Mandate → x402 settle → receipt, using a mock x402 facilitator

### v0.13 — ERC-8004 + AGNTCY bridges
**Goal**: NTH DAO reputation becomes portable to the open agent web.

- `nth_dao/bridge/erc8004.py` — publish `AchievementCredential` digest
  on-chain; supports Base + Ethereum mainnet + Optimism + Polygon
- `nth_dao/bridge/ipfs.py` — content-address the full credential
- `nth_dao/agntcy/manifest.py` — publish DAO-level manifest in AGNTCY shape
- Documentation: "How to take your NTH DAO reputation portable"
- Tests: bridge calls go through `unittest.mock` for chain; we don't
  ship live-chain CI

---

## Part 6 — Decision points the maintainers need to make

Before v0.10 starts, two design calls that should be explicit:

### Decision 1: Mandate signer = wallet or runtime?

**Option A**: Browser wallet (current `crypto.ts`) signs every Mandate.
The user clicks "Approve" in the web console for each settlement.

**Option B**: Local runtime signs autonomously within Mandate
constraints (`max_amount`, `expiry`, `allowed_counterparties`).

A2A / AP2 industry direction is **B** for sub-threshold amounts and
**A** for everything above a per-DAO threshold. Recommend matching that
norm: configurable `auto_approve_max` per DAO, defaulting to **0**
(everything needs the wallet) so we're safe-by-default.

### Decision 2: Settlement adapter selection at DAO or at agent level?

**Option A**: The DAO declares its accepted settlement methods at
bootstrap; every agent in the DAO uses them.

**Option B**: Each agent declares its own settlement preferences; the
counterparty chooses among the intersection.

A2A / AP2 industry direction is **B** because it lets specialized
agents (e.g. a translation agent that only accepts USDC, vs. a code
review agent that takes both card and USDC) coexist in the same DAO.
Recommend **B**, with the DAO setting a *default* that agents can
override.

---

## Part 7 — What this means for our current PR + issue triage

- **Andy's PR #7 EventBus** (just merged as `0a39d3c`) becomes the audit
  spine for everything in this design. Good investment in retrospect.
- **The multi-DAO sidebar** (just merged as `a1cb4e8 / 95a035d / 5f617c6`)
  becomes the "settlement scope" — different DAOs can have different
  settlement adapters configured.
- **AchievementCredential** (merged earlier as part of `1f60002`) already
  has the bridge-friendly shape; v0.13 just adds a publish call.
- **MumoLawOS** (the user's example DAO) becomes a natural first
  customer for Mandate-based legal-tech transactions.

---

## Sources

- [A2A Protocol Explained — Stellagent](https://stellagent.ai/insights/a2a-protocol-google-agent-to-agent)
- [A2A v1.0 spec](https://a2a-protocol.org/latest/)
- [AP2 (Agent Payments Protocol) — Google Cloud Blog](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol)
- [AP2 Explained — eco.com](https://eco.com/support/en/articles/15192002-ap2-protocol-explained-google-s-agentic-commerce-standard-2026)
- [Improving A2A — Sensitive Data, arXiv 2505.12490](https://arxiv.org/pdf/2505.12490)
- [AGNTCY / Linux Foundation](https://www.linuxfoundation.org/press/linux-foundation-welcomes-the-agntcy-project-to-standardize-open-multi-agent-system-infrastructure-and-break-down-ai-agent-silos)
- [AGNTCY docs](https://docs.agntcy.org/)
- [x402 Protocol / Coinbase](https://docs.cdp.coinbase.com/x402/welcome)
- [x402 v2 multi-chain — eco.com](https://eco.com/support/en/articles/12328618-x402-protocol-explained-how-ai-agents-pay-onchain)
- [ERC-8004 + x402 stack — RNWY](https://rnwy.com/blog/erc-8004-x402-identity-payment-stack)
- [ERC-8004 explainer — eco.com](https://eco.com/support/en/articles/13221214-what-is-erc-8004-the-ethereum-standard-enabling-trustless-ai-agents)
- [Skyfire KYAPay + KYA framework — Stellagent](https://stellagent.ai/insights/skyfire-kyapay-know-your-agent)
- [Mastercard Verifiable Intent](https://www.mastercard.com/global/en/news-and-trends/stories/2026/verifiable-intent.html)
- [Verifiable Intent + AP2 — FIDO Alliance](https://fidoalliance.org/building-the-trust-layer-for-agentic-payments-with-ap2-and-verifiable-intent/)
- [Visa Trusted Agent Protocol — FinTech Wrapup](https://www.fintechwrapup.com/p/deep-dive-mastercard-verifiable-intent)
- [LangGraph vs CrewAI vs AutoGen — 2026 comparison](https://pecollective.com/blog/ai-agent-frameworks-compared/)
- [Best Multi-Agent Frameworks 2026 — gurusup](https://gurusup.com/blog/best-multi-agent-frameworks-2026)
- [AI Agents with DIDs and VCs — arXiv 2511.02841](https://arxiv.org/abs/2511.02841)
- [Virtuals + ERC-8183](https://www.datawallet.com/crypto/what-is-virtuals-protocol)
- [Agentic AI in DeFi — Olas/Fetch.ai context](https://medium.com/thecapital/agentic-ai-in-defi-the-dawn-of-autonomous-on-chain-finance-584652364d08)
