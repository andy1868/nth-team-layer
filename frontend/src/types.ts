export type TeamRole = "owner" | "admin" | "member" | "guest";

export type TeamConfig = {
  team_name: string;
  join_policy: string;
  member_ids: string[];
  admin_ids: string[];
  roles: Record<string, TeamRole>;
};

export type Summary = {
  team: TeamConfig;
  workspace: string;
  workspace_is_local?: boolean;
  members: number;
  channels: number;
  tasks: number;
  online_agents: number;
  active_missions: number;
  blackboard_entries: number;
  server_time: string;
  actor_code?: string;   // v0.9.8: the caller's stable visible handle
};

// v0.9.8: result of `GET /api/agents/by_code/{code}` — used by the
// "add agent by code" search box.
export type CodeLookupResult = {
  code: string;
  agent_id: string;
  pubkey_hex: string;
  source: "home" | "group";
  role: TeamRole | string;
  group_slug?: string;
};

export type Actor = {
  agent_id: string;
  role: TeamRole;
  code?: string;       // v0.9.8: visible Telegram-style handle
};

export type Member = {
  agent_id: string;
  role: TeamRole;
  online: boolean;
  code?: string;       // v0.9.8
  pubkey_hex?: string; // present for group-DAO members
};

export type Channel = {
  channel_id: string;
  name: string;
  topic: string;
  is_private: boolean;
  member_ids: string[];
  created_by: string;
  created_at: string;
};

export type Message = {
  message_id: string;
  channel_id: string;
  sender_id: string;
  body: string;
  created_at: string;
};

export type Announcement = {
  announcement_id: string;
  channel_id: string;
  author_id: string;
  title: string;
  body: string;
  created_at: string;
};

export type TaskStatus = "open" | "accepted" | "running" | "blocked" | "completed" | "cancelled";

export type DaoTask = {
  task_id: string;
  channel_id: string;
  created_by: string;
  assignee_id: string;
  title: string;
  description: string;
  status: TaskStatus;
  due_at: string;
  created_at: string;
  updated_at: string;
};

export type AuditEvent = {
  event_id: string;
  event_type: string;
  actor_id: string;
  target_type: string;
  target_id: string;
  summary: string;
  created_at: string;
  metadata: Record<string, unknown>;
};

export type DaoState = {
  team: TeamConfig;
  actor: Actor;
  members: Member[];
  channels: Channel[];
  messages: Message[];
  announcements: Announcement[];
  tasks: DaoTask[];
  audit: AuditEvent[];
  dao?: DaoMeta;
  active_channel_id?: string;
};

// v0.9.7 - one agent ↔ many DAOs. Each DAO is either the local "home"
// workspace or a registered Group from the cross-workspace GroupRegistry.
export type DaoKind = "home" | "group";

export type DaoSummary = {
  slug: string;
  display_name: string;
  kind: DaoKind;
  group_id: string;
  description: string;
  policy: string;
  joined: boolean;
  member_count: number;
  admin_count?: number;
};

export type DaoMeta = {
  slug: string;
  kind: DaoKind;
  display_name: string;
  group_id: string;
  description: string;
  policy: string;
  member_count: number;
  admin_count?: number;
  founder_pubkey?: string;
};

// v0.10 T-9: Mandate sidebar shapes. These mirror the JSON returned
// by `_summarise_*` in nth_dao/web/__init__.py. Keep field names in
// sync - the sidebar parses the JSON as `MandateListing` directly
// (no client-side normalisation layer).

export type MandateKind = "intent" | "cart" | "payment";

export type MandateAmount = {
  currency: string;
  value: string;
};

export type IntentMandateSummary = {
  kind: "intent";
  digest: string;
  issuer: string;
  agent: string;
  purpose: string;
  max_amount: MandateAmount;
  expires_at: string;
  expired: boolean;
  allowed_counterparties: string[];
  allowed_settlement_methods: string[];
};

export type CartMandateSummary = {
  kind: "cart";
  digest: string;
  issuer: string;
  intent_digest: string;
  total: MandateAmount;
  settlement_methods: string[];
  expires_at: string;
  expired: boolean;
  line_item_count: number;
};

export type PaymentMandateSummary = {
  kind: "payment";
  digest: string;
  issuer: string;
  cart_digest: string;
  payee: string;
  settlement_choice: string;
  issued_at: string;
  expires_at: string;
  expired: boolean;
};

export type MandateListing = {
  intents: IntentMandateSummary[];
  carts: CartMandateSummary[];
  payments: PaymentMandateSummary[];
};

export type MandateVerifyCheck = {
  name: string;
  ok: boolean;
  reason?: string;
};

export type MandateVerifyResult = {
  ok: boolean;
  reason: string;
  checks: MandateVerifyCheck[];
};
