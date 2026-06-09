<!--
  License: MIT (see LICENSE at repository root)
  Copyright (c) 2026 AlexNthLab and NTH DAO contributors

  This document and its appendices are part of the NTH DAO protocol
  specification surface. Wire-format changes documented here SHOULD
  follow the same versioning discipline as the code: additive
  changes bump the spec minor; breaking changes bump the major and
  invalidate signatures of the previous schema.
-->

# NTH DAO — Design Trade-offs & Limitations

**License:** MIT — see `LICENSE` at the repository root for the full
text and copyright notice.

> *"Bitcoin gave you absolute sovereignty over money — at the cost of
> absolute responsibility for keeping your keys. NTH DAO gives you
> absolute sovereignty over your work history — at the same cost,
> and a few of its own. This document lists those costs, plainly."*

---

## 0. Why this document exists

NTH DAO was first pitched as **"a decentralized AI Agent network"**.
That framing is wrong, and it carries marketing risk: it invites
comparisons with frameworks (Autogen, LangGraph, CrewAI) that
NTH DAO does not in fact replace, and it implicitly promises
autonomous AI primitives that the underlying cryptography does not
deliver.

The accurate framing is:

> **NTH DAO is local-first developer infrastructure that lets a
> human user accumulate a cryptographically signed, AI-tool-rotation-
> resistant record of their work — with explicit, documented
> boundaries on what that record proves.**

Identity belongs to the **human user**, not to the AI model. The
models change every six months; the user does not. NTH DAO's
records survive across `Claude 3.7 → 4.7 → 4.8 → …` precisely
because the identity primitive (Ed25519 keypair + did:key) is bound
to the user side of that interaction.

This shifts NTH DAO's competitive frame from "AI agent network" to
"the next-generation Git/ORCID": a personal-and-cryptographic record
of what the user accomplished, with which tools, when. That frame
is smaller in TAM but considerably more defensible — and forces us
to be honest about what NTH DAO cannot do.

The four trade-offs below are NOT bugs. They are deliberate design
choices that follow from the local-first + identity-on-user
posture. Each section lists:

- **Promise** — what NTH DAO commits to delivering
- **Limit** — where the commitment stops
- **Reasoning** — why this boundary, not somewhere else
- **V1 contract** — what ships today
- **V2 path** — where the boundary could move when the cost of
  moving it justifies the work

### V2 priority ordering (explicit)

Each section below sketches a V2 path. They are NOT equal. The
ordering reflects how directly each V2 work item addresses NTH
DAO's foundational claim ("local-first sovereign work-history
record"):

> **TLSNotary / TEE attestation (Tool-provenance, §3 V2) >
> Receipt-side cap_token chain (Autonomous signing, §2 V2) >
> Identity-migration tooling (Key-loss, §1 V2) >
> OS-level multi-tenant helpers (Workspace sharing, §4 V2)**

The ordering above uses `>` (strict precedence) deliberately, not
`≫` (much-greater-than). Each tier is more important than the next
but they are not orders of magnitude apart — every V2 item is
genuinely foundational and a maintainer choosing between §2 and §3
should not feel that picking §2 is "wrong".

Reasoning, in plain terms:

- **§3 V2 is highest** because the consistency-game model has a
  visible expiration date (Appendix C trigger). When the 2030-ish
  statistical-death event arrives, the foundational trust claim
  of NTH DAO ("you can defend your assertion chain against
  fabrication") collapses. We must have a successor (TLSNotary /
  TEE attestation) in flight by then or NTH DAO degrades into
  "social trust only" — survivable, but no longer the strongest
  claim. This is the only V2 path where doing nothing changes
  what the protocol fundamentally offers.

- **§2 V2 is second** because the cap_token receipt-chain is
  pure protocol extension: the cryptography is well-understood
  (we already ship `cap_token.py`), the work is ~100 LOC, and
  it unlocks autonomous-agent use cases that real users will
  start hitting in v1.x. High leverage, low surface area.

- **§1 V2 is third** because key loss is a real risk but
  affects single users, not the protocol's claim-strength. The
  manifest-based "physical break + social pivot" path in §1 V1
  is honest and sufficient until enough users actually lose
  keys to demand better recovery.

- **§4 V2 is last** because shared-workstation multi-human use
  is already solvable by Linux operations (separate `unix`
  accounts, per-user `~/.nth-dao`). NTH DAO doesn't need to
  re-invent OS-level isolation. The work item is documentation
  + a setup-helper script — useful but not foundational.

A maintainer reading this who has time for exactly one V2 piece
should pick §3 (TLSNotary / TEE). A v1.x sprint can pick up §2
inside a normal iteration.

---

## 1. Private-key loss is a one-way disaster

### Promise

NTH DAO gives you absolute sovereignty over your work record:
your `<workspace>/.nth/identity.json` holds an Ed25519 private
key under `0600` permissions, and nothing in the protocol depends
on a recovery oracle, a custody service, or a friendly third
party. The records you sign with this key are yours, and only
yours, to extend.

### Limit

Losing the private key is **catastrophic and irreversible**.
NTH DAO does not attempt cryptographic continuation of identity
under a new keypair. The chain of receipts signed by the lost
key remains immutable and addressable, but it stops growing —
and no new key can claim to be the "same" DID without breaking
the local-first trust model.

### Reasoning — why we don't ship Shamir's Secret Sharing in V1

The obvious proposal (and we considered it carefully) is `2-of-3
Shamir's Secret Sharing` across the user's trusted devices
(phone + backup server + a second NTH node). It looks attractive
on paper. Three concrete problems killed it for V1:

1. **Collusion is not abstract.**
   Two share holders can combine without the user's consent. If
   the user splits across `phone + iCloud backup admin + spouse's
   laptop`, then `iCloud + spouse` is a real coalition. SSS
   protects against single-point loss; it does not protect
   against two-of-three coalitions, which in domestic and small-
   business settings are common.

2. **Choice of shard holders leaks the social graph.**
   The identities of the three shard holders are themselves
   sensitive metadata. An attacker who can compel disclosure of
   shard storage providers learns the user's trust network — the
   exact thing local-first was supposed to keep private.

3. **Recovery has no public trust anchor.**
   Bitcoin sidesteps this because keys sign *transactions*: the
   result (a moved UTXO) is the evidence. NTH DAO keys sign
   *identity continuity*: the result of a recovered key is "I am
   the same person who signed the past 1000 receipts." Without a
   Trent — and we don't have one — anyone can claim "I just
   recovered Tony's key via SSS" and start signing on his
   behalf. There is no protocol-level way to prove that the
   recovered key is the original.

The honest assessment: SSS is the right *direction* for V2, but
it requires either (a) a registry of shard holders with their
own crypto attestations, (b) hardware-enclave-bound shards, or
(c) social proofs distributed at shard-creation time. All three
add complexity that needs real user demand to justify.

### V1 contract — physical break + social pivot

When a user loses their key, NTH DAO offers no automated
recovery. The pragmatic path:

1. Generate a new keypair (`did:key:zNew`).
2. Publish an **identity migration manifest** (spec sketch in
   Appendix A) signed by the new key, listing the old DID,
   external accounts (GitHub, X, email) that controlled the old
   identity, and signed statements from those accounts asserting
   "same human."
3. Consumers reading old-DID receipts SEE the migration manifest
   on the new DID's well-known endpoint and **decide for
   themselves** whether to extend trust to the new DID.

Crucially: the migration is **non-transitive** and **subjective**.
There is no protocol-wide consensus that "old_did == new_did".
Each consumer makes their own decision based on whichever social
proofs they trust. This is the Web-of-Trust model, applied to
identity continuity rather than introductions.

The old DID's receipts remain valid and addressable forever.
They are a closed chapter — like a deceased developer's GitHub
profile. The new DID starts a new chapter, with continuity
asserted but not cryptographically proven.

**Trade-off in plain terms:** accept partial loss of trace
rather than allow a forged continuation. The hardest part of
this commitment is psychological, not technical: the user must
internalize that a key-loss event resets their accumulated
reputation, the way a stolen passport resets a traveler's
verified-identity bona fides.

### V2 path

When NTH DAO has both enough real users to justify the work AND
a credible plan for one of (a)/(b)/(c) above, V2 could add
optional SSS with explicit operator-acknowledged collusion risk
disclosure. This is not on the v1.0 roadmap.

---

## 2. Autonomous AI signs via ephemeral DID + capability delegation

### Promise

NTH DAO can record cryptographically valid receipts even for AI
actions that take place while the user is offline (e.g. an
overnight code-refactoring agent, a scheduled report generator).
The receipts chain back to the user's root authority through a
short-lived delegated key — preserving "identity belongs to the
user" without forging a "user was online" claim.

### Limit

The receipt's *direct signer* is an ephemeral DID, not the user.
The chain back to the user's root authority is via a separate
`cap_token` envelope. Consumers that don't speak the NTH
extension see only "some DID signed this content" without
understanding why it should be trusted as the user's work.

This is a soft fork from motebit's pure `signer_did → sig` model.
Motebit verifiers will accept the bytes (the Ed25519 signature
is valid), but they won't know to chain through the cap_token to
the user. The trust-extension semantics are NTH-only.

### Reasoning

Capability delegation is a battle-tested cryptographic pattern
(Macaroons, OAuth scopes, SPKI/SDSI, biscuit tokens). The model:

```
[User Root Key]                       Long-lived; held in
       │                              <workspace>/.nth/identity.json
       │
       ▼  user pre-authorizes
[Ephemeral Subject DID]                Short-lived; generated by the
       │                               autonomous process; lives in
       │   only for: code-refactor     memory; discarded at expiry.
       │   until:  next 08:00
       ▼
[Receipt signed by ephemeral]          Signature is real Ed25519,
       │                               just not the root key.
       │
       └─── attaches: authorizing_cap_token from User Root
                       (NTH receipts reference it; motebit verifiers ignore)
```

An external auditor reading an NTH-aware verifier sees:

> *"At 03:00 UTC, ephemeral DID `did:key:zXYZ` signed a receipt
> claiming refactor work on goal G. That signature is verifiable.
> The ephemeral DID was authorized by user `did:key:zTony` via
> cap_token X, valid 22:00-08:00, scoped to capability
> `nth:receipt_sign` and goal G. cap_token X is itself a valid
> Ed25519 signature from `did:key:zTony`. Therefore: this is
> Tony's work, performed by a process Tony authorized, within
> the time and scope window Tony specified."*

A motebit-only verifier sees just the first sentence and stops.
That's acceptable — motebit interop is about wire-format
compatibility for the core receipt, not about NTH's trust-
extension semantics.

### V1 contract

`nth_dao/cap_token.py` already supports the issuer→subject→
capabilities→scope→expiry model — that work shipped in commit
`99e40da`. The receipt-side extension (this afternoon's work)
requires three additions:

1. **New capability `nth:receipt_sign`** added to
   `KNOWN_CAPABILITIES`. This is the most powerful cap NTH DAO
   exposes — it lets the bearer assert "I did X" claims that
   chain back to the user's reputation. Issuers must consciously
   grant it.

2. **New optional field on the receipt envelope:**
   `authorizing_cap_token` (the full token dict, base64url is
   not necessary at the receipt layer since it's already JSON).

3. **`verify_receipt` extension:** when `signer_did` doesn't
   match an expected root, and `authorizing_cap_token` is
   present, run the verification chain:
   - cap_token's signature verifies under issuer_did's pubkey ✓
   - cap_token's `subject_did` equals receipt's `signer_did` ✓
   - cap_token's `not_after >= receipt timestamp` ✓
   - cap_token's `capabilities` includes `nth:receipt_sign` ✓
   - If `scope_task_id` is set, it equals the receipt's `goal_id` ✓
   - **Revocation is NOT consulted at this layer.** See the
     "Revocation semantics" subsection below.

### Revocation semantics — non-retroactive (normative)

When a cap_token is revoked at time `T_revoke`:

- Receipts signed by that cap_token at time `T_sig < T_revoke`
  **remain valid forever**. The cryptographic act of signing was
  legal at the time it happened; revocation cannot rewrite
  history.
- API calls authenticated by that cap_token at time
  `T_call >= T_revoke` are rejected at middleware (the existing
  `revoked_set()` check in `nth_dao/cap_token.py`).
- `verify_receipt` MUST NOT consult the revocation list when
  validating a chained receipt. The receipt's
  `authorizing_cap_token` is verified against the cap_token's
  own internal time bounds (`not_before` / `not_after`), not
  against current revocation status.

The reasoning is the "permanent trace" promise of NTH DAO: if
revoking a cap_token retroactively invalidated months of past
autonomous-agent work, every NTH DAO user would have to choose
between forgiving compromised delegations (risky) and losing
their accumulated work history (catastrophic). Non-retroactive
revocation is the only choice consistent with NTH DAO's identity
posture.

The alternative semantics (revocation invalidates past
receipts) is well-defined and useful in OAuth-style scope
revocation, but does not fit this protocol. Implementers porting
NTH DAO to a context where retroactive revocation IS appropriate
(e.g. a regulated financial setting) need to fork the verify
semantics consciously, not by accident.

Estimated diff: ~100 LOC across `cap_token.py`,
`execution_receipt.py`, and the verifier test suite.

**Implementation tracking:** **shipped** as part of the same
commit that updates this section (and as a follow-up to D1, the
chain-link entry from §1 V1.x-graduation lands together). See
`nth_dao/execution_receipt.py::_verify_cap_token_chain_for_receipt`
and the test suite at
`tests/test_receipt_cap_chain_and_chaining.py`. If implementation
drifts from this section's semantics in a future change, this
document is the contract that wins and the implementation must
be amended to match.

**R1 footnote — time anchor on cap_token chain verification:**
the initial implementation of `_verify_cap_token_chain_for_receipt`
called `verify_cap_token` without `now_ms_override`, so the
inner time check used current wall clock. That contradicts the
D7 non-retroactive semantic: a receipt signed at time T while
the cap was valid would, years later, fail to verify because
the cap has since expired. The fix anchors the time check to
the receipt's `issued_at`. Regression test:
`test_c2_d7_receipt_signed_within_window_verifies_after_cap_expired`.

### V2 path — multi-hop delegation

V1 supports single-hop delegation (user → ephemeral). Real
agent orchestration may want chained delegation (user → planner
agent → executor agent), where each hop adds a cap_token to a
chain. This is the Macaroons "third-party caveat" model and is
well-understood, but adds verification complexity and is out of
scope until a real use case appears.

---

## 3. Tool provenance: assertion chain, not third-party fact

### Promise

NTH DAO produces an immutable, cryptographically signed chain of
the user's stated work history. Each receipt asserts "I used
tool T at time S to produce output O." The chain is tamper-proof:
once signed, no one can alter past entries without invalidating
the user's signature.

### Limit

The receipt asserts that the user *invoked* a specific AI tool.
It does **not** prove the tool actually ran, nor that the output
recorded came from the tool versus from the user's keyboard.

Concretely: if Tony writes a 200-line Python function himself
and then signs a receipt saying "produced by Claude Code,
duration 14.2s, prompt-hash abc123," NTH DAO cannot detect the
lie. The cryptography proves Tony signed the receipt. It does
not prove Anthropic's servers were involved.

This is fundamentally different from motebit-style receipts of
on-chain transactions, where the result (state change) is
evidence of the work. NTH DAO records *human-AI workflow steps*,
which lack a public oracle of "did this actually happen."

### Reasoning — the consistency-game economic model

The mitigation NTH DAO bets on is **long-term cost asymmetry**:

- *Lying once is technically possible.* Tony fabricates one
  receipt; no immediate detection.
- *Lying consistently is statistically detectable.* Across
  hundreds of receipts, fabricated "Claude output" diverges
  from real Claude output along measurable axes: stylometric
  fingerprints, response-time distribution, error patterns,
  context-coherence with prior receipts in the same goal.
- *Detection invalidates the entire identity.* The chain rooted
  in a single Ed25519 key means: once Tony is caught fabricating
  one receipt, every receipt that key signed becomes suspect.
  His accumulated reputation — every signed contribution to
  every project — collapses to zero.

This is the same Schelling fence that keeps lawyers from
forging notarized documents: technically possible, economically
ruinous when caught.

### The 2030 sunset (verifiable)

The consistency-game model has a known expiration date because
AI-output and human-output are converging. By some near-future
date, no statistical test will reliably distinguish them; the
"detection invalidates identity" leg of the argument loses its
teeth.

We refuse to leave the sunset as a vague "around 2030." Instead
we propose a falsifiable trigger:

> **Sunset condition:** when at least two independent, open
> benchmarks of AI-vs-human output indistinguishability show
> consistent detection accuracy below 5% (i.e. essentially
> random) over three consecutive quarterly evaluations, the
> consistency-game model is declared expired.

When that condition fires:

1. The NTH DAO README and protocol docs update to reflect that
   tool-provenance claims are *social trust only*, with no
   statistical detection backstop.
2. New receipts MAY include `consistency_game_expired: true` in
   metadata so downstream consumers can adjust their trust
   weighting.
3. Pre-expiry receipts retain whatever trust the consumer chooses
   to assign them based on the era they were signed in.

Concrete benchmark candidates suitable as the two required
independent sources: an HELM-style AI-detection eval, a
peer-reviewed paper-or-better stylometric study. The exact list
will be maintained at `docs/consistency_game_sunset_benchmarks.md`
when V1 ships.

### V2 path — TLSNotary, TEE, model-side attestation

The cryptographically rigorous solution requires the AI vendor
(Anthropic, OpenAI) to attest cryptographically that the output
was generated by their model. The two viable paths:

- **TLSNotary**: a Mozilla-funded protocol where TLS connections
  can produce post-hoc proofs that specific request/response
  bytes were exchanged with a specific server. Works without
  vendor cooperation.
- **Trusted Execution Environment (TEE) attestation**: the
  vendor runs inference inside an SGX/SEV enclave and signs the
  output. Requires vendor cooperation but yields the strongest
  guarantees.

Both add real complexity and cost. NTH DAO V1 explicitly does
not include them; V2 may, when the consistency-game sunset
becomes imminent.

---

## 4. Workspace = single human in V1

### Promise

NTH DAO v1 supports one user with arbitrarily many delegated
automation subjects via `cap_token`. A single developer can have
their workspace + their nightly code-refactor agent + their
weekend review agent + their assistant-for-PR-triage, each with
its own ephemeral DID, its own scope, its own time window — all
chained back to the developer's root identity.

### Limit

NTH DAO v1 does **not** support multiple humans sharing a single
workspace. Two developers who both want to write receipts signed
by their own identities cannot do so against the same
`<workspace>/.nth/identity.json` — that file holds exactly one
keypair.

### Reasoning — why env var switching is not a solution

The naive proposal is to switch identities via an environment
variable:

```bash
NTH_USER_DID=did:nth:alice nth-...
```

This is **configuration, not isolation**. Any process running in
the same workspace can read or set `NTH_USER_DID`. A malicious
agent (or buggy script) could steal Alice's identity context and
sign as Alice. For genuine multi-tenant deployments, two paths
are real:

- **OS-level isolation**: separate Unix accounts, each with
  their own `~/.nth-dao/workspaces/<...>` and `chmod 600`
  private keys. Collaboration happens at the DAO/A2A protocol
  layer, not in shared local files.
- **Process-level isolation with session tokens**: each user
  authenticates against a workspace-resident keyring service
  and receives a session token; all signing operations go
  through the service. This is more flexible but adds
  significant infrastructure.

### V1 contract

The single-keypair-per-workspace assumption is held to honestly:

- One workspace = one human (the operator/admin).
- That human can delegate via `cap_token` to N ephemeral
  subjects (multi-subject single-human is **fully supported**).
- Two humans wanting to collaborate use **two workspaces** and
  collaborate at the A2A protocol layer. They have their own
  Unix accounts, their own private keys, their own keyrings.

This is sufficient for the realistic v1 deployment shape: an
independent developer plus their automation, not a shared team
workstation.

### V2 path

Real multi-human-sharing requires OS-level user isolation
(option 1 above). The user sets up two Unix accounts and gets
two workspaces. What V2 would add is documentation, possibly a
helper CLI for the setup, and clear messaging that this is the
supported model.

**Operational constraint — port allocation.** Two NTH DAO
workspaces on the same host cannot both bind the default
console port (`NTH_PORT=8080`) or share the same mDNS
responder slot. Multi-human-on-same-host deployments MUST:

- Set `NTH_PORT` to a distinct value per workspace (e.g.
  `NTH_PORT=18080` for Alice, `NTH_PORT=18081` for Bob).
- Either keep `NTH_LAN_PUBLISH=0` for at least one of them, OR
  ensure the mDNS responder is configured with workspace-
  distinct service names so two responders on the same host
  don't advertise as the same NTH DAO node.

This is a documentation and tooling task for V2 — the
underlying code already accepts `NTH_PORT` overrides; what's
missing is the helper script that walks an operator through
"add a second user" without footguns.

The session-token approach (option 2) is more invasive and
appropriate only when (a) operating-system isolation isn't
available (e.g. a single-user macOS where families share an
account), or (b) team-level deployment becomes a primary use
case. Neither is the v1 audience.

---

# Appendix A — Identity migration manifest (sketch, v1)

When a user loses their key and starts over, they publish at
`/.well-known/nth-dao/identity-migration.json` a manifest like:

```json
{
  "kind": "nth-identity-migration-v1",
  "spec": "nth-dao/identity-migration@1.0",
  "old_did": "did:key:z6Mk<OLD>",
  "new_did": "did:key:z6Mk<NEW>",
  "old_sig": null,
  "new_sig": "<base64url Ed25519 signature by new key over canonical_json(this dict excluding new_sig)>",
  "external_proofs": [
    {
      "platform": "github",
      "url": "https://gist.github.com/<user>/<gist>",
      "asserts": "old_did and new_did belong to the same human (me)"
    },
    {
      "platform": "x",
      "url": "https://x.com/<user>/status/<id>",
      "asserts": "I lost access to <old_did> and switched to <new_did>"
    },
    {
      "platform": "email",
      "fingerprint": "<PGP key fingerprint>",
      "asserts": "<see signed mail>"
    }
  ],
  "issued_at": 1751337600000,
  "expires_at": null
}
```

Semantics (deliberately minimal):

- `old_sig` is **JSON null** when the old key is unrecoverable
  (deliberate signal, distinct from "field forgot to be set").
  If the user has partial control of the old key (e.g.
  recovered via partial SSS shards, or briefly recovered a
  backup), they include the actual Ed25519 signature as a hex
  string; consumers MAY use a non-null `old_sig` as a stronger
  proof of identity continuity than the social proofs alone.
- `external_proofs` is **opt-in social trust**. Each consumer
  independently decides which platforms they trust to make
  "same human" claims.
- The manifest is **non-transitive**. Consumer A trusting
  `old_did → new_did` does not imply Consumer B does. There is
  no protocol-wide registry of migrations.
- The migration record is itself signed receipt material under
  `new_did`. It joins the new DID's receipt chain. So a
  fabricated migration is detectable the same way fabricated
  receipts are (consistency game; see §3).

NTH DAO tooling SHOULD surface the migration when:

- A consumer queries the public-identity endpoint of `old_did`
  via `/.well-known/nth-dao/identity.json`, and the operator has
  forwarding configured.
- A consumer encounters a receipt-link to `old_did` and wants
  to verify whether the work continues under a new identity.

What NTH DAO MUST NOT do:

- Automatically merge `old_did` and `new_did` reputations in
  any shared registry.
- Hide the discontinuity from consumers.
- Allow `old_sig` to be silently fabricated; absence is signaled
  by the empty string, never by a missing field.

---

# Appendix B — Capability vocabulary

### Naming convention (read before extending)

Capability strings follow the pattern
``<namespace>:<action>``. The action part has historically
varied across namespaces and we have deliberately chosen NOT
to retrofit consistency:

- ``a2a:`` namespace uses **noun_verb** form (
  ``message_send``, ``task_get``, ``task_cancel``,
  ``task_split``). This mirrors A2A's own JSON-RPC method
  shape (``message/send``, ``tasks/get``).
- ``nth:`` namespace uses **verb_noun** form (
  ``post_message``, ``add_member``, ``receipt_sign``,
  ``rotate_key``). This was the convention established by the
  earliest NTH-native capabilities and is preserved for
  backwards compatibility.

Consumers writing capability strings by hand should consult
the table below rather than guessing. The inconsistency is
a real ergonomic gotcha; a future major version may unify
the two patterns, but a unification would be a breaking
change that invalidates all in-flight cap_tokens, so the
current generation explicitly tolerates the asymmetry.

### Current `KNOWN_CAPABILITIES` (as of commit `99e40da`):

| Capability | Granted authority |
|---|---|
| `a2a:message_send` | Call A2A `message/send` to append messages to a Task |
| `a2a:task_get` | Call A2A `tasks/get` to retrieve Task state |
| `a2a:task_cancel` | Call A2A `tasks/cancel` to terminate a Task |
| `a2a:task_split` | Call A2A `tasks/split` to materialize a Task as a structured Mission |
| `nth:post_message` | POST `/api/messages` (NTH-native chat) |
| `nth:add_member` | POST `/api/agents/add` (admin-grade) |

Added by §2 of this document (afternoon-commit pending):

| Capability | Granted authority |
|---|---|
| `nth:receipt_sign` | Sign motebit-compatible receipts that chain back to the issuer via this cap_token |

`nth:receipt_sign` is the most powerful capability NTH DAO
exposes. A bearer can assert "I did X" claims under the user's
extended reputation. Issuers must:

- Use the shortest possible TTL (default 1 hour; absolute max
  via `MAX_TTL_MS` is 1 week).
- Use `scope_task_id` whenever the work has a known target.
- Never grant this capability to a third party they do not
  fully trust to act on their behalf — the receipts will be
  attributed (chain-extended) to the issuer.

Future capability candidates (not yet implemented; listed for
forward planning):

- `nth:rotate_key` — limited-scope key rotation authority
- `nth:delegate_further` — allows the subject to issue further
  cap_tokens (delegated-delegation; not in v1 to keep the
  privilege ladder flat)
- `nth:read_receipts` — read-only access to receipt history,
  useful for audit tooling

---

# Appendix C — Consistency-game sunset criteria

The "tool provenance is socially-defensible, not cryptographically
proven" argument (§3) depends on a measurable claim: that
AI-generated and human-generated content can be statistically
distinguished. When that claim ceases to hold, the argument
expires.

To make the sunset falsifiable rather than rhetorical, the
following protocol determines when it has fired:

**Monitored benchmarks** — the v1 monitored set, locked in this
specification:

- **B-1: LMSYS Chatbot Arena — Hard Prompt Subset / Human
  Distinguishability Rate.** Senior developer blind-evaluations
  of "human-original refactor" vs "advanced-LLM-agent
  refactor". The metric is the experts' error rate at
  distinguishing the two sources. We pick LMSYS because its
  evaluation methodology is community-vetted and its dataset
  refreshes alongside model releases — giving us a sunset
  signal that tracks real model evolution rather than a stale
  benchmark.

- **B-2: SWE-bench / LiveCodeBench — Stylometric Delta.**
  Structural-fingerprint overlap between AI-generated patches
  and a controlled human-expert corpus on the same issues.
  We pick SWE-bench / LiveCodeBench specifically because
  code's structural fingerprint (cyclomatic-complexity
  distribution, identifier-naming entropy, refactor-pattern
  reuse) is the technical domain NTH DAO consumers care most
  about — failure on this axis is the most consequential
  collapse of the consistency-game model.

- **B-3 (supplementary): HELM — Safety & Stylometry variant.**
  Holistic Evaluation of Language Models, specifically the
  modules that compare model output to human-written controls
  on the same prompts. Used as a tie-breaker / cross-check
  against B-1 and B-2; not by itself a trigger.

**Statistical floor** (minimum sample size — D2 spec hole fix,
non-negotiable):

> A benchmark "evaluation" only counts toward the trigger if
> the evaluation reports at least **N = 1000** independent
> trials. Below this floor, "detection accuracy < 5%" is
> within Bernoulli noise and triggers spuriously.
>
> For the small-N case where a benchmark publishes
> qualitative summaries rather than per-trial accuracy, the
> evaluation does NOT count — the trigger requires
> quantitative per-trial detection rates.

**Evaluation-event cadence** (what "consecutive" means — D3
spec hole fix):

> Benchmarks publish on irregular cadences. To prevent the
> trigger from being silently blocked by a dormant benchmark,
> a "quarterly evaluation event" is defined as:
>
> > **the latest publicly published evaluation of that
> > benchmark within the trailing 120 days**.
>
> Three "consecutive" events MUST be separated by at least
> 75 days each (so a benchmark publishing weekly cannot fast-
> forward the trigger; consecutive events are spaced like
> real quarterly cadences). Events from the same publication
> wave do NOT count as consecutive.
>
> If a benchmark has had no publication within 240 days, it
> is considered "dormant" and its events stop counting until
> a new publication restores cadence.

**Trigger condition** (final wording):

> The sunset is declared fired when ANY of these
> cross-validation combinations holds, with every contributing
> evaluation event satisfying both the statistical floor
> (N ≥ 1000) and the cadence rules above:
>
> - **B-1** (LMSYS expert distinguishability) AND **B-2**
>   (LiveCodeBench stylometric overlap) each show
>   **detection accuracy below 5%** (i.e. essentially random
>   discrimination) for **three consecutive evaluation
>   events** each, OR
> - **B-1** alone shows the same condition for **four**
>   consecutive evaluation events (i.e. single-benchmark
>   confirmation with extended duration), OR
> - **B-3** (HELM stylometry variant) corroborates **either**
>   B-1 or B-2 for three consecutive evaluation events on
>   each side.

The "any of" disjunction is deliberate: two independent
benchmarks agreeing for three events is the cleanest
falsification path, but a single benchmark sustaining the
condition for an extra event is strong enough evidence on
its own; conversely, HELM's holistic methodology corroborating
either primary benchmark closes the same loop from a different
angle. We did not want the trigger to fail to fire just
because one specific benchmark went dormant or was retired by
its maintainers.

**Governance — who declares the sunset has fired** (D4 spec
hole fix):

NTH DAO has no central authority and the protocol does not
delegate sunset declaration to one. The sunset trigger is
designed as **an objective condition that any participant can
evaluate independently**:

- Any consumer monitoring the benchmark feeds applies the
  condition above to the publicly available evaluation data
  and decides, FOR THEMSELVES, whether the sunset has fired
  and how to adjust their trust weighting of NTH DAO
  receipts going forward.
- Receipt issuers MAY include
  ``consistency_game_expired: true`` in receipt metadata
  once they personally believe the trigger has fired. This is
  voluntary signaling, not a protocol-mandated field.
- The maintainers of this whitepaper SHOULD record the date
  of their own determination in
  ``docs/sunset_declarations/<YYYY-MM-DD>.md`` as a public
  reference point. **This is a content-level statement, not a
  protocol authorization** — other consumers are free to
  reach a different conclusion based on the same data.

This is the same non-transitive subjective-trust posture used
for identity migration in §1 V1: NTH DAO refuses to centralize
truth-of-state declarations that are not cryptographically
self-evident. The sunset, like a key migration, is something
each consumer recognizes on their own.

**On firing:**

1. This document updates `§3` to mark the consistency-game
   argument as expired and remove the "long-term cost
   asymmetry" reasoning from the trust model.
2. The protocol adds an optional receipt-envelope field
   `consistency_game_expired: true` so receipts post-sunset
   carry the signal. Pre-sunset receipts are unchanged.
3. Downstream consumers SHOULD adjust their trust weighting of
   receipt-asserted tool provenance; reasonable defaults move
   from "qualified social trust" to "pure social trust."
4. NTH DAO project communication (README, blog posts,
   documentation) updates to reflect the new equilibrium.

**Importantly**, the sunset does NOT invalidate pre-sunset
receipts. They retain whatever reputation they accumulated
during the era when consistency detection was viable. The
sunset only affects the trust calculus for new receipts and
forward-looking consumer behavior.

The next-generation solutions (TLSNotary, TEE attestation; see
§3 V2 path) become the recommended provenance backbones after
sunset. Whether they are adopted is, as always, a deliberate
choice for whoever ships NTH DAO v2, not a protocol-mandated
upgrade path.

---

# Appendix D — External chain-head snapshotting (V1.x candidate)

The chain integrity work shipped in V1 (`prev_content_hash`
linking, `verify_receipt_chain` walker, `head_content_hash`
store helper) defeats third-party tampering and silent omission
of intermediate receipts. It does **not** defeat the signer
rewriting their own history, because the signer controls the
keypair.

The mitigation is external snapshotting — a consumer (or a
service NTH DAO interoperates with) records the signer's current
chain head at a known time T. Later, if the signer publishes a
chain that doesn't contain that head, the recorded snapshot is
itself evidence of inconsistency.

Three plausible V1.x designs, in increasing complexity:

  1. **Voluntary peer mirroring** — NTH DAO nodes that learn
     of each other via mDNS optionally exchange chain-head
     records. Each peer records the OTHER's head with a
     timestamp. Pure-peer model, no central party. Limit:
     evidence quality depends on the witness's own honesty
     and the timestamp's verifiability.

  2. **Lightweight public timestamping** — emit chain-head
     records via an external timestamping service that can
     attest "I saw this hash at time T" (e.g. OpenTimestamps,
     a Mastodon-style public posting). No specific service
     mandated; the receipt envelope grows an optional
     `chain_head_witnesses` array referring to URLs / IDs.

  3. **Trusted-witness consortium / blockchain anchor** —
     periodically commit chain heads to a more durable
     witness layer (blockchain, transparency log). Highest
     guarantee, highest infrastructure dependency. We list
     this for completeness but explicitly DO NOT recommend
     it for v1.x — it imports too much external complexity
     for a use case (developer reputation chains) that does
     not need block-finality-grade integrity.

The v1.x track should ship (1) first as a low-cost pilot, then
evaluate whether (2) is warranted before any consideration of
(3). The V2 priority ordering in §0 puts this behind the §3
TLSNotary/TEE work since chain-integrity-without-witnesses is
still enough for most use cases NTH DAO actually serves.

---

# Closing — what NTH DAO is, after this document

> *Not* a decentralized AI agent network.
> *Not* a self-sovereign-identity replacement for OAuth.
> *Not* a cryptographic proof that any specific AI ran.

> *Is* a local-first cryptographic ledger of the user's stated
> work history, with documented honest boundaries on what the
> ledger proves, designed to outlive the specific AI tools the
> user touches along the way.

Git did not prove that the author wrote the code, only that
they committed it under a verifiable identity. NTH DAO extends
the same posture to the AI-collaboration era: we record, with
cryptographic discipline, *that the user used these tools to
produce this work, on this date* — and we let the long arc of
the user's coherent contribution chain speak for the quality of
the underlying claim.

When a future reader, decades from now, reads NTH DAO receipts,
what they should be able to trust is:

- The user controlled the keypair when each receipt was signed
- Each individual receipt's `timeline` is internally hash-
  consistent (motebit-style per-receipt `content_hash` over the
  timeline; tampering with any entry invalidates the receipt)
- The user *said* tool T was used at time S
- For autonomous entries, the user pre-authorized the delegated
  signer with explicit scope and time bounds (see §2)
- **Per-signer chain integrity** (D1 V1.x graduated in commit
  `ee6bf60`): a third party holding a complete corpus of one
  signer's receipts can run `verify_receipt_chain(corpus)` and
  detect (a) a missing receipt that other receipts reference
  via `prev_content_hash`, (b) a fork where two receipts share
  the same prev pointer, (c) more than one genesis, or (d) any
  individual receipt's signature being invalid. This was
  initially listed under "NOT promised" in earlier drafts of
  this section but shipped in V1 — the line is moved.

What they should NOT believe NTH DAO ever promised:

- That Anthropic / OpenAI / any AI vendor cryptographically
  attested to running their model
- That the recorded outputs are statistically indistinguishable
  from what the model would have produced
- That the user could not, with sufficient effort, have
  fabricated some entries
- **That a signer cannot rewrite their own history.** Chain
  integrity (above) defeats THIRD-PARTY tampering and silent
  omission of intermediate receipts. It does **not** defeat
  the signer who controls their own keypair: they can re-sign
  the entire chain forward with different `prev_content_hash`
  values, different `issued_at` strings, even different
  `timeline` contents. The only check against this is
  **external snapshotting** — a consumer who recorded the
  signer's chain head at time T can later prove what existed
  at T, even if the signer subsequently substitutes a
  different chain. External snapshotting is a V1.x candidate
  (see Appendix D, below), not part of V1.

This is what honest infrastructure looks like.
