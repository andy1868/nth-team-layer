// chat-native contact / discover / group APIs for v0.9.6.
// Pure TS - no React imports here, so it can be unit-tested without a DOM.

export interface AgentMatch {
  agent_id: string;
  score: number;
  status: string;
  hostname: string;
  backend_id: string;
  capabilities: string[];
  groups: string[];
  last_seen: string;
  matched: string[];
}

export interface LANPeer {
  agent_id: string;
  label: string;
  capabilities: string[];
  groups: string[];
  ws_url: string;
  pubkey_hex: string;
  source_addr: string;
  rtt_ms: number;
}

export interface UniqueGroup {
  group_id: string;
  slug: string;
  display_name: string;
  description: string;
  policy: "open" | "approval" | "closed" | "voted";
  founder_pubkey: string;
  member_pubkeys: string[];
  admin_pubkeys: string[];
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  signer_pubkey: string;
  sig: string;
}

export interface PolicyProposal {
  proposal_id: string;
  group_id: string;
  proposer_pubkey: string;
  proposed_policy: "open" | "approval" | "closed" | "voted";
  proposed_add_members: string[];
  proposed_remove_members: string[];
  proposed_display_name: string | null;
  rationale: string;
  created_at: string;
  expires_at: string;
  votes: Array<{
    voter_pubkey: string;
    choice: "yes" | "no" | "abstain";
    voted_at: string;
    sig: string;
  }>;
  proposer_sig: string;
  resolved?: { passed: boolean; reason: string };
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init?.headers ?? {})
    }
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = (await response.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else detail = JSON.stringify(data.detail);
    } catch {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

// ── search registered agents (search by name) ──

export async function searchAgents(query: string, limit = 10): Promise<AgentMatch[]> {
  const q = query.trim();
  if (!q) return [];
  const params = new URLSearchParams({ q, limit: String(limit) });
  const data = await jsonRequest<{ results: AgentMatch[] }>(
    `/api/agents/search?${params.toString()}`
  );
  return data.results;
}

// ── LAN discovery (people nearby) ──

export async function discoverLanPeers(opts: {
  timeoutSeconds?: number;
  psk?: string;
  wantedCapabilities?: string[];
} = {}): Promise<LANPeer[]> {
  const data = await jsonRequest<{ peers: LANPeer[] }>("/api/agents/lan_discover", {
    method: "POST",
    body: JSON.stringify({
      timeout_seconds: opts.timeoutSeconds ?? 2,
      psk: opts.psk ?? "",
      wanted_capabilities: opts.wantedCapabilities ?? []
    })
  });
  return data.peers;
}

// ── add agent (add contact) ──

export async function addAgent(input: {
  actorId: string;
  targetAgentId?: string;
  targetDid?: string;
  label?: string;
}): Promise<{ ok: boolean; agent_id: string; did: string; label: string }> {
  return jsonRequest("/api/agents/add", {
    method: "POST",
    body: JSON.stringify({
      actor_id: input.actorId,
      target_agent_id: input.targetAgentId ?? "",
      target_did: input.targetDid ?? "",
      label: input.label ?? ""
    })
  });
}

// ── groups (group chats tab) ──

export async function listGroups(): Promise<UniqueGroup[]> {
  const data = await jsonRequest<{ groups: UniqueGroup[] }>("/api/groups/registry");
  return data.groups;
}

export async function searchGroups(
  query: string,
  policy?: string,
  limit = 10
): Promise<UniqueGroup[]> {
  const data = await jsonRequest<{ results: UniqueGroup[] }>(
    "/api/groups/registry/search",
    {
      method: "POST",
      body: JSON.stringify({ query, policy: policy ?? null, limit })
    }
  );
  return data.results;
}

// Step 1 of group creation. Returns a server-prepared unsigned skeleton
// the client must sign locally before posting to /publish.
export async function prepareGroup(input: {
  actorId: string;
  actorPubkeyHex: string;
  displayName: string;
  description?: string;
  policy?: "open" | "approval" | "closed" | "voted";
}): Promise<{ slug: string; unsigned_record: UniqueGroup; to_sign: UniqueGroup }> {
  return jsonRequest("/api/groups/registry", {
    method: "POST",
    body: JSON.stringify({
      actor_id: input.actorId,
      actor_pubkey_hex: input.actorPubkeyHex,
      display_name: input.displayName,
      description: input.description ?? "",
      policy: input.policy ?? "open"
    })
  });
}

// Step 2: post the signed record.
export async function publishGroup(record: UniqueGroup): Promise<UniqueGroup> {
  return jsonRequest("/api/groups/registry/publish", {
    method: "POST",
    body: JSON.stringify({ record })
  });
}

// ── governance ──

export async function prepareProposal(input: {
  groupId: string;
  actorPubkeyHex: string;
  newPolicy?: "open" | "approval" | "closed" | "voted";
  addMemberPubkeys?: string[];
  removeMemberPubkeys?: string[];
  newDisplayName?: string;
  rationale?: string;
  ttlDays?: number;
}): Promise<{ unsigned_proposal: PolicyProposal; to_sign: unknown }> {
  return jsonRequest(`/api/groups/registry/${input.groupId}/proposals`, {
    method: "POST",
    body: JSON.stringify({
      actor_pubkey_hex: input.actorPubkeyHex,
      group_id: input.groupId,
      new_policy: input.newPolicy ?? null,
      add_member_pubkeys: input.addMemberPubkeys ?? [],
      remove_member_pubkeys: input.removeMemberPubkeys ?? [],
      new_display_name: input.newDisplayName ?? null,
      rationale: input.rationale ?? "",
      ttl_days: input.ttlDays ?? 7
    })
  });
}

export async function publishProposal(
  groupId: string,
  proposal: PolicyProposal
): Promise<PolicyProposal> {
  return jsonRequest(`/api/groups/registry/${groupId}/proposals/publish`, {
    method: "POST",
    body: JSON.stringify({ proposal })
  });
}

export async function listProposals(groupId: string): Promise<PolicyProposal[]> {
  const data = await jsonRequest<{ proposals: PolicyProposal[] }>(
    `/api/groups/registry/${groupId}/proposals`
  );
  return data.proposals;
}

export async function castSignedVote(
  groupId: string,
  proposalId: string,
  vote: { voter_pubkey: string; choice: "yes" | "no" | "abstain"; voted_at: string; sig: string }
): Promise<{ proposal: PolicyProposal; resolved: { passed: boolean; reason: string } }> {
  return jsonRequest(
    `/api/groups/registry/${groupId}/proposals/${proposalId}/sign_vote`,
    {
      method: "POST",
      body: JSON.stringify({ vote })
    }
  );
}
