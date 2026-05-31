# NTH DAO Wire Protocols (v1)

This is the protocol-level spec — what's on the wire, what's in the files —
so a Rust / Go / TypeScript implementation can interop with the Python
reference. If you're consuming `nth_dao` as a Python library, you don't need
this document.

All `bytes` payloads are encoded as hex strings in JSON form.
All signatures are Ed25519 over `canonical_json(payload_without_sig)`.
"canonical JSON" means `json.dumps(d, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` then UTF-8 encoded.

---

## 1. Identity

A NTH DAO agent identity is an Ed25519 keypair plus a stable `agent_id`.

When the agent_id is "cryptographic" (`AgentID.is_cryptographic = True`)
it is derived as:

    agent_id = SHA-256(pubkey_bytes).hex()[:12]

Plain `agent_id`s (no derivation) are also allowed for legacy use; they
have no cryptographic backing and rely on operator trust.

### identity.json

```json
{
    "agent_id":      "9c50edd2373f",
    "label":         "Alice's laptop",
    "is_cryptographic": true,
    "pubkey":        "<64 hex chars = 32 bytes>",
    "fingerprint":   "<sha256(pubkey || agent_id)[:16] hex>",
    "metadata":      { ... },
    "private_key":   "<64 hex chars = 32 bytes>"
}
```

`private_key` MUST be readable only by the file owner. The Python
reference implementation enforces this via `chmod 0o600` on POSIX and
`icacls /grant ... /inheritance:r` on Windows; failures emit a logger
warning.

**Load-time validation**: an implementation MUST verify that
`Ed25519SigningKey(private_key).verify_key.encode() == pubkey`. If not,
refuse to load (the file has been tampered with).

---

## 2. Gossip (WebSocket P2P)

### Handshake (challenge-response)

Both sides MUST have a crypto identity (PyNaCl / Ed25519 equivalent).

```
client → server: { "type": "hello", "agent_id": "<id>", "pubkey_hex": "<hex>" }
server → client: { "type": "challenge", "nonce": "<32 hex chars>" }
client → server: { "type": "challenge_response",
                   "agent_id": "<id>",
                   "sig": "<hex sig of canonical_json({\"nonce\": <nonce>})>" }
server: verify sig under client's claimed pubkey; if fail, close 1008.
server → client: { "type": "welcome",
                   "agent_id": "<server id>",
                   "pubkey_hex": "<server pubkey>",
                   "channels":   [...],
                   "server_challenge": "<32 hex chars>" }
```

After welcome, both sides treat the connection as authenticated. The
server pins (agent_id → pubkey_hex) in its trust anchor map for the
duration; the client adds the server to its own map (TOFU).

### Reject conditions (server)
- Missing `agent_id` or `pubkey_hex` in hello → close 1008.
- A peer claims an `agent_id` that's already in the trust map under a
  *different* pubkey → close 1008 ("pubkey mismatch with trust anchor").
- `challenge_response.sig` doesn't verify under claimed pubkey → 1008.
- Handshake doesn't complete within 8 seconds → close.

### Gossip message envelope

```json
{
    "type":    "gossip",
    "message": { ... ChannelMessage ... }
}
```

Where `ChannelMessage` is:

```json
{
    "msg_id":       "<hex>",
    "channel":      "team" | "group:<name>" | "dm:<a>--<b>",
    "from_agent":   "<author agent_id>",
    "content":      "...",
    "content_type": "text" | "markdown" | "json",
    "reply_to":     "<msg_id or '' >",
    "mentions":     ["<agent_id>", ...],
    "timestamp":    "<ISO 8601>",
    "metadata":     { ... },
    "sig":          "<hex sig over canonical_json(message_without_sig)>"
}
```

### Receive validation (peer)

For every incoming `gossip` envelope, in order:

1. **Dedup**: drop if `msg_id` is in recent-seen LRU (default cache 1000).
2. **Timestamp window**: drop if `now - timestamp > 600s` (replay) or
   `timestamp - now > 60s` (clock skew tolerance).
3. **Trust anchor lookup**: find the pubkey for `from_agent` —
   - direct lookup in pinned `trusted_pubkeys`, OR
   - transitive lookup via `TrustGraph.trusted_pubkey_for(from_agent,
     max_depth=2)` if `trust_graph` is configured.
4. **Signature**: verify `sig` over `canonical_json(message − sig)` under
   that pubkey. **Verification failure MUST drop the message** —
   never `pass`, never relay.
5. **Persist**: append the message to local `team_messages/<channel>/...jsonl`.
6. **Relay**: forward the original envelope to every other connected peer
   (`exclude` the peer we received it from).

---

## 3. Endorsements & Revocations (Web-of-Trust)

### Endorsement

```json
{
    "endorser_pubkey":  "<hex>",
    "subject_pubkey":   "<hex>",
    "subject_agent_id": "<id>",
    "depth_allowed":    1..5,
    "context":          "general" | "code_review" | ...,
    "issued_at":        "<ISO>",
    "expires_at":       "<ISO>",
    "sig":              "<hex over canonical_json(endorsement − sig)>"
}
```

Stored append-only at `team_trust/endorsements.jsonl`.

`depth_allowed` is a hop-budget cap: when verifying transitive trust,
the subject of this endorsement can re-endorse at most
`depth_allowed - 1` further hops.

### Revocation

```json
{
    "endorser_pubkey":         "<hex>",
    "subject_pubkey":          "<hex>",
    "subject_agent_id":        "<id>",
    "endorsement_issued_at":   "<ISO matching the endorsement>",
    "reason":                  "...",
    "revoked_at":              "<ISO>",
    "sig":                     "<hex over canonical_json(revocation − sig)>"
}
```

Stored append-only at `team_trust/revocations.jsonl`. To take effect:

1. Signature MUST verify under `endorser_pubkey`.
2. A matching `endorsement` MUST exist locally — otherwise the revocation
   is dropped (preventing pre-emptive DoS revocations of endorsements
   that don't exist).

### BFS resolution

Roots are pinned in `team_trust/roots.json`:

```json
{ "<agent_id>": "<pubkey_hex>", ... }
```

To check `is_trusted(agent_id, pubkey, max_depth=N)`:

1. If `(agent_id, pubkey)` is in roots → trusted (depth 0).
2. BFS from each root; at each hop, follow non-revoked, non-expired
   endorsements; subject's further-hop budget is
   `min(remaining_budget, endorser.depth_allowed - 1)`.
3. Stop when target found or hop budget exhausted.

Max absolute depth is capped at `MAX_PROPAGATION_DEPTH = 5` even if the
caller passes a larger value.

---

## 4. Signed Team Config

`team.json` (one per workspace):

```json
{
    "team_id":      "<short id>",
    "team_name":    "...",
    "join_policy":  "open" | "approval" | "invite_only" | "token",
    "join_token":   "...",
    "admin_ids":    [...],
    "member_ids":   [...],
    "roles":        { "<agent_id>": "owner|admin|member|guest" },
    "created_at":   "<ISO>",
    "metadata":     { ... },
    "owner_pubkey":   "<hex>",       // optional; if set, file is signed
    "owner_sig":      "<hex>",       // sig over canonical_json(cfg − owner_sig)
    "sig_updated_at": "<ISO>"
}
```

When `owner_pubkey` is non-empty, `owner_sig` MUST verify. A reader that
sees an invalid signature MUST treat the file as if it didn't exist
(empty TeamConfig) — this is what prevents git-sync poisoning.

The signature is over the dict with `owner_sig` removed (the
"signable dict"), encoded as canonical JSON.

---

## 5. Invitations

A signed bundle that lets a new agent bootstrap into a team with one
scan / paste / link.

URL form: `nthdao+invite://<base64url(json_payload)>`

Payload:

```json
{
    "team_id":      "...",
    "team_name":    "...",
    "owner_pubkey": "<hex>",
    "issuer":       "<owner agent_id>",
    "issued_at":    "<ISO>",
    "expires_at":   "<ISO>",
    "join_token":   "...",       // for JoinPolicy.TOKEN
    "ws_url":       "ws://host:9876",  // optional bootstrap peer
    "psk":          "...",       // optional LAN discovery PSK
    "sig":          "<hex over canonical_json(payload − sig)>"
}
```

Validation:
1. `expires_at` must be in the future.
2. `sig` must verify under `owner_pubkey`.
3. Payload after base64-decode must be JSON object, ≤ 2048 bytes.

---

## 6. LAN Discovery (UDP)

Default port `9877` on the local subnet. JSON over UDP. Single-packet only
(MAX_MESSAGE_BYTES = 4096).

### Query

```json
{
    "type":    "nth-dao-query",
    "v":       1,
    "from":    "<querier agent_id>",
    "wants":   ["python", ...],   // empty = match anyone
    "nonce":   "<16 hex chars>",
    "psk_tag": "<hmac_sha256(psk, nonce).hex() or ''>"
}
```

### Hello (response)

```json
{
    "type":         "nth-dao-hello",
    "v":            1,
    "agent_id":     "<responder id>",
    "label":        "...",
    "capabilities": [...],
    "groups":       [...],
    "ws_url":       "ws://host:9876",
    "pubkey_hex":   "<hex>",
    "metadata":     { ... },
    "nonce":        "<echoes the query nonce>",
    "psk_tag":      "<HMAC-SHA256(psk, nonce).hex() or ''>",
    "ts":           <epoch float>
}
```

### Responder rules
- Drop queries with `v != 1`, wrong `type`, or `from == own agent_id`.
- If `psk` is configured, drop queries whose `psk_tag` doesn't match
  `HMAC-SHA256(psk, nonce)` under constant-time comparison.
- If `wants` is non-empty and not a subset of own capabilities, don't
  respond.

### Querier rules
- Drop hellos with mismatched `nonce`, wrong `v`, wrong `type`.
- If `psk` is configured, drop hellos whose `psk_tag` doesn't match.
- Deduplicate by `agent_id`.
- Tolerate `ConnectionResetError` on `recvfrom` (Windows ICMP
  unreachable bleed-through).

---

## 7. Marketplace Order

`team_marketplace/<order_id>.json`:

```json
{
    "order_id":    "<hex>",
    "creator":     "<agent_id>",
    "title":       "...",
    "description": "...",
    "context":     "general" | "code_review" | ...,
    "reward":      <float>,
    "deadline":    "<ISO or '' >",
    "tags":        [...],
    "requirements": {
        "min_reputation": <float>,
        "capabilities":   [...]
    },
    "status":      "open"|"claimed"|"in_progress"|"submitted"|"completed"|"failed"|"cancelled"|"disputed"|"expired",
    "claimant":    "<agent_id or '' >",
    "submission_proof": "...",
    "submission_at":    "<ISO>",
    "rating":      <float 0-5>,
    "feedback":    "...",
    "created_at":  "<ISO>",
    "claimed_at":  "<ISO>",
    "completed_at": "<ISO>",
    "timeline":    [{ "action": "...", "actor": "...", "timestamp": "...", ... }, ...],
    "creator_sig":  "<hex>",     // signed at create
    "claimant_sig": "<hex>"      // signed at claim
}
```

Credit transactions logged append-only at
`team_marketplace/<agent>_credits.ledger.jsonl`, one line per movement:

```json
{
    "ts":             "<ISO>",
    "agent_id":       "...",
    "kind":           "escrow_lock|escrow_refund_cancel|escrow_refund_expired",
    "delta":          <signed float>,
    "balance_before": <float>,
    "order_id":       "..."
}
```

---

## 8. Mission

`missions/<mission_id>.json`:

```json
{
    "id":          "<hex>",
    "title":       "...",
    "goal":        "...",
    "status":      "planning"|"active"|"paused"|"completed"|"failed"|"cancelled",
    "owner":       "<agent_id>",
    "scope":       "shared"|"group:<g>"|"private:<id>",
    "steps":       [ MissionStep, ... ],
    "deadline":    "<ISO or '' >",
    "priority":    "low"|"normal"|"high"|"critical",
    "tags":        [...],
    "metadata":    { ... },
    "created_at":  "<ISO>",
    "updated_at":  "<ISO>",
    "completed_at": "<ISO or null>"
}
```

`MissionStep`:

```json
{
    "id":          "<hex>",
    "description": "...",
    "status":      "todo"|"claimed"|"active"|"done"|"failed"|"handed_off"|"blocked",
    "required_capabilities": [...],
    "inputs":      { ... },
    "output":      { ... } | null,
    "depends_on":  ["<step_id>", ...],
    "assignee":    "<agent_id>",
    "previous_assignees": [...],
    "created_at":  "<ISO>",
    "updated_at":  "<ISO>",
    "completed_at": "<ISO or null>",
    "notes":       ["[<ISO>] <author>: <text>", ...]
}
```

### Claim rules (atomic)

- Implementations MUST acquire an exclusive file lock (POSIX `fcntl.LOCK_EX`
  or Windows `msvcrt.LK_NBLCK` on `<mission_id>.json.lock`) before
  read-modify-write.
- A `try_claim(mission_id, step_id, agent_id, capabilities)` MUST:
  1. Reject if step status ∉ {`todo`, `handed_off`, `blocked`}.
  2. Reject if step.assignee is non-empty AND ≠ agent_id (handed_off
     with a specific recipient only allows that recipient to claim).
  3. Reject if step.required_capabilities ⊄ agent.capabilities.
  4. Otherwise transition step → `active`, assignee = agent_id, add a
     note, save.
- Concurrent claimers MUST observe exactly-once semantics: one winner,
  all others receive a `ClaimConflict` (Python) / equivalent error.

### Terminal state rules

- All steps `done` (or `handed_off`) → mission `completed`.
- Any step `failed` AND no remaining actionable step (i.e. no step
  satisfies its `depends_on` set and is still `open`) → mission `failed`.
- Otherwise PLANNING → ACTIVE on first non-TODO step transition.

---

*Last updated for nth-dao v0.9.1.*
