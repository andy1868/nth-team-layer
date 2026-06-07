import type {
  CodeLookupResult,
  DaoState,
  DaoSummary,
  DaoTask,
  MandateKind,
  MandateListing,
  MandateVerifyResult,
  Message,
  Summary,
  TaskStatus
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
      const data = (await response.json()) as { detail?: string };
      detail = data.detail ?? detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export function getSummary(actorId: string = ""): Promise<Summary> {
  const qs = actorId ? `?actor_id=${encodeURIComponent(actorId)}` : "";
  return request<Summary>(`/api/summary${qs}`);
}

// v0.9.8: look up an agent by their 8-hex visible code, e.g. "a3f7-b2e8".
// Used by the "add agent by code" search box.
export function lookupAgentByCode(code: string): Promise<CodeLookupResult> {
  return request<CodeLookupResult>(`/api/agents/by_code/${encodeURIComponent(code)}`);
}

export function getState(agentId: string, channelId: string): Promise<DaoState> {
  const params = new URLSearchParams({ agent_id: agentId, channel_id: channelId });
  return request<DaoState>(`/api/state?${params.toString()}`);
}

// v0.9.7 - multi-DAO endpoints (sidebar list + per-DAO scoped state)
export function getDaos(actorId: string, actorPubkeyHex: string): Promise<{ daos: DaoSummary[] }> {
  const params = new URLSearchParams({ actor_id: actorId, actor_pubkey_hex: actorPubkeyHex });
  return request<{ daos: DaoSummary[] }>(`/api/daos?${params.toString()}`);
}

export function getDaoState(slug: string, agentId: string, channelId: string): Promise<DaoState> {
  const params = new URLSearchParams({ agent_id: agentId });
  if (channelId) params.set("channel_id", channelId);
  return request<DaoState>(`/api/daos/${encodeURIComponent(slug)}/state?${params.toString()}`);
}

export function join(agentId: string): Promise<{ ok: boolean; agent_id: string; reason: string }> {
  return request("/api/join", {
    method: "POST",
    body: JSON.stringify({ agent_id: agentId })
  });
}

export function createChannel(input: {
  actorId: string;
  name: string;
  topic: string;
  isPrivate: boolean;
  channelId?: string;
}): Promise<unknown> {
  return request("/api/channels", {
    method: "POST",
    body: JSON.stringify({
      actor_id: input.actorId,
      name: input.name,
      topic: input.topic,
      is_private: input.isPrivate,
      channel_id: input.channelId ?? ""
    })
  });
}

export function postMessage(input: {
  agentId: string;
  channelId: string;
  body: string;
}): Promise<Message> {
  return request<Message>("/api/messages", {
    method: "POST",
    body: JSON.stringify({
      agent_id: input.agentId,
      channel_id: input.channelId,
      body: input.body
    })
  });
}

export function postAnnouncement(input: {
  authorId: string;
  channelId: string;
  title: string;
  body: string;
}): Promise<unknown> {
  return request("/api/announcements", {
    method: "POST",
    body: JSON.stringify({
      author_id: input.authorId,
      channel_id: input.channelId,
      title: input.title,
      body: input.body
    })
  });
}

export function createTask(input: {
  createdBy: string;
  channelId: string;
  title: string;
  description: string;
  assigneeId: string;
}): Promise<DaoTask> {
  return request<DaoTask>("/api/tasks", {
    method: "POST",
    body: JSON.stringify({
      created_by: input.createdBy,
      channel_id: input.channelId,
      title: input.title,
      description: input.description,
      assignee_id: input.assigneeId
    })
  });
}

export function updateTaskStatus(input: {
  taskId: string;
  actorId: string;
  status: TaskStatus;
}): Promise<DaoTask> {
  return request<DaoTask>(`/api/tasks/${encodeURIComponent(input.taskId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      actor_id: input.actorId,
      status: input.status
    })
  });
}

// v0.10 T-9: Mandate sidebar API. Backend lives at `nth_dao/web/__init__.py`;
// see the `_summarise_*` helpers for field semantics. The four calls form
// the entire surface the sidebar needs: list, fetch one, persist, verify.

/**
 * Read the three mandate kinds for sidebar rendering.
 *
 * V-28: the backend now gates every /api/mandates/* route through the
 * membership check. The frontend MUST thread an actor_id so the
 * gate sees a non-anonymous caller; otherwise the backend uses the
 * default admin id which fails on non-admin workspaces.
 */
function mandateActorQuery(actorId: string): string {
  if (!actorId.trim()) {
    throw new Error("actorId is required for mandate API calls");
  }
  return `?actor_id=${encodeURIComponent(actorId)}`;
}

export function listMandates(actorId: string): Promise<MandateListing> {
  return request<MandateListing>(`/api/mandates${mandateActorQuery(actorId)}`);
}

/** Fetch the full canonical mandate body (e.g. for a wallet-side re-verify). */
export function getMandate(
  kind: MandateKind, digest: string, actorId: string
): Promise<unknown> {
  return request<unknown>(
    `/api/mandates/${kind}/${encodeURIComponent(digest)}${mandateActorQuery(actorId)}`
  );
}

/**
 * Persist a signed mandate so it appears in the sidebar.
 *
 * Server re-derives the digest from the canonical JSON of the body,
 * so callers cannot pin a wrong digest. The returned `digest` is the
 * authoritative key the sidebar uses in subsequent /verify and /get
 * calls.
 */
export function storeMandate(
  kind: MandateKind,
  mandate: unknown,
  actorId: string
): Promise<{ ok: boolean; kind: MandateKind; digest: string }> {
  if (!actorId.trim()) {
    throw new Error("actorId is required for mandate API calls");
  }
  return request("/api/mandates/store", {
    method: "POST",
    body: JSON.stringify({ kind, mandate, actor_id: actorId })
  });
}

/**
 * Run server-side signature + expiry + binding checks.
 *
 * When verifying a Cart, pass the Intent it claims to bind to via
 * `againstIntent` to also gate the digest-binding and constraint
 * checks. A Payment is not valid standalone: callers must pass both
 * `againstCart` and the Cart's bound `againstIntent`, otherwise the
 * server returns ok=false.
 */
export function verifyMandate(input: {
  kind: MandateKind;
  mandate: unknown;
  againstIntent?: unknown;
  againstCart?: unknown;
  actorId: string;
}): Promise<MandateVerifyResult> {
  if (!input.actorId.trim()) {
    throw new Error("actorId is required for mandate API calls");
  }
  return request<MandateVerifyResult>("/api/mandates/verify", {
    method: "POST",
    body: JSON.stringify({
      kind: input.kind,
      mandate: input.mandate,
      against_intent: input.againstIntent,
      against_cart: input.againstCart,
      actor_id: input.actorId
    })
  });
}
