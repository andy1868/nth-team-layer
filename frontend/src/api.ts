import type { DaoState, DaoSummary, DaoTask, Message, Summary, TaskStatus } from "./types";

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

export function getSummary(): Promise<Summary> {
  return request<Summary>("/api/summary");
}

export function getState(agentId: string, channelId: string): Promise<DaoState> {
  const params = new URLSearchParams({ agent_id: agentId, channel_id: channelId });
  return request<DaoState>(`/api/state?${params.toString()}`);
}

// v0.9.7 — multi-DAO endpoints (sidebar list + per-DAO scoped state)
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
