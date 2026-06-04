import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  createChannel,
  createTask,
  getDaoState,
  getDaos,
  getSummary,
  join,
  lookupAgentByCode,
  postAnnouncement,
  postMessage,
  updateTaskStatus
} from "./api";
import { ContactShell } from "./panels";
import { type BrowserWallet, loadOrCreateWallet } from "./crypto";
import type { DaoState, DaoSummary, Summary, TaskStatus } from "./types";

const defaultAgent = window.localStorage.getItem("nth-dao-agent-id") || "admin";
const defaultDao = window.localStorage.getItem("nth-dao-active-slug") || "home";
const taskStatuses: TaskStatus[] = ["open", "accepted", "running", "blocked", "completed", "cancelled"];

// DAO-scoped channel IDs carry a `dao-<slug>-` prefix server-side. When
// creating a channel from inside a group DAO, the UI prepends the slug
// so the channel lands in the right scope.
function scopedChannelId(slug: string, bare: string): string {
  if (!bare) return bare;
  if (!slug || slug === "home") return bare;
  const prefix = `dao-${slug}-`;
  return bare.startsWith(prefix) ? bare : prefix + bare;
}

function App() {
  const [agentId, setAgentId] = useState(defaultAgent);
  const [activeDao, setActiveDao] = useState<string>(defaultDao);
  const [daos, setDaos] = useState<DaoSummary[]>([]);
  const [selectedChannel, setSelectedChannel] = useState<string>("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [state, setState] = useState<DaoState | null>(null);
  const [messageBody, setMessageBody] = useState("");
  const [announcementTitle, setAnnouncementTitle] = useState("");
  const [announcementBody, setAnnouncementBody] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskDescription, setTaskDescription] = useState("");
  const [taskAssignee, setTaskAssignee] = useState("");
  const [channelName, setChannelName] = useState("");
  const [channelTopic, setChannelTopic] = useState("");
  const [notice, setNotice] = useState("Loading console state...");
  const [busy, setBusy] = useState(false);
  const [wallet, setWallet] = useState<BrowserWallet | null>(null);
  const [walletError, setWalletError] = useState<string | null>(null);
  // v0.9.8: agent-code lookup form (the Telegram-style "add by username" box)
  const [lookupCode, setLookupCode] = useState("");
  const [lookupResult, setLookupResult] = useState<string>("");

  // Load (or generate on first run) the browser-resident Ed25519 wallet.
  // Private key stays inside IndexedDB as non-extractable CryptoKey.
  useEffect(() => {
    let cancelled = false;
    loadOrCreateWallet()
      .then((w) => { if (!cancelled) setWallet(w); })
      .catch((e: Error) => { if (!cancelled) setWalletError(e.message); });
    return () => { cancelled = true; };
  }, []);

  const activeChannel = useMemo(
    () => state?.channels.find((channel) => channel.channel_id === selectedChannel),
    [selectedChannel, state?.channels]
  );

  async function refresh(
    nextAgent: string = agentId,
    nextChannel: string = selectedChannel,
    nextDao: string = activeDao,
  ) {
    const cleanAgent = nextAgent.trim() || "admin";
    const [summaryData, stateData] = await Promise.all([
      getSummary(cleanAgent),
      getDaoState(nextDao, cleanAgent, nextChannel),
    ]);
    setSummary(summaryData);
    setState(stateData);
    if (!nextChannel && stateData.active_channel_id) {
      setSelectedChannel(stateData.active_channel_id);
    }
    setNotice("Ready");
  }

  async function refreshDaos(pubkeyHex: string) {
    try {
      const list = await getDaos(agentId, pubkeyHex);
      setDaos(list.daos);
    } catch (e) {
      // Sidebar failures shouldn't break the main view; surface as notice.
      setNotice((e as Error).message);
    }
  }

  useEffect(() => {
    refresh().catch((error: Error) => setNotice(error.message));
    refreshDaos(wallet?.pubkeyHex ?? "");
    const id = window.setInterval(() => {
      refresh().catch((error: Error) => setNotice(error.message));
    }, 5000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDao, wallet?.pubkeyHex]);

  function switchDao(slug: string) {
    if (slug === activeDao) return;
    setActiveDao(slug);
    setSelectedChannel("");      // let backend pick default channel for this DAO
    window.localStorage.setItem("nth-dao-active-slug", slug);
  }

  async function run(action: () => Promise<void>, done = "Updated") {
    setBusy(true);
    setNotice("Working...");
    try {
      await action();
      await refresh();
      setNotice(done);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  function updateAgent(value: string) {
    setAgentId(value);
    window.localStorage.setItem("nth-dao-agent-id", value);
  }

  async function onJoin(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await join(agentId.trim() || "admin");
    }, "Agent joined");
  }

  async function onCreateChannel(event: FormEvent) {
    event.preventDefault();
    const scopedId = scopedChannelId(activeDao, channelName.trim());
    await run(async () => {
      await createChannel({
        actorId: agentId,
        name: channelName,
        topic: channelTopic,
        isPrivate: false,
        channelId: scopedId,
      });
      setChannelName("");
      setChannelTopic("");
    }, "Channel created");
  }

  async function onPostMessage(event: FormEvent) {
    event.preventDefault();
    if (!messageBody.trim()) return;
    await run(async () => {
      await postMessage({ agentId, channelId: selectedChannel, body: messageBody });
      setMessageBody("");
    }, "Message posted");
  }

  async function onPostAnnouncement(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await postAnnouncement({
        authorId: agentId,
        channelId: selectedChannel,
        title: announcementTitle,
        body: announcementBody
      });
      setAnnouncementTitle("");
      setAnnouncementBody("");
    }, "Announcement posted");
  }

  async function onCreateTask(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await createTask({
        createdBy: agentId,
        channelId: selectedChannel,
        title: taskTitle,
        description: taskDescription,
        assigneeId: taskAssignee
      });
      setTaskTitle("");
      setTaskDescription("");
      setTaskAssignee("");
    }, "Task created");
  }

  async function onUpdateTask(taskId: string, status: TaskStatus) {
    await run(async () => {
      await updateTaskStatus({ taskId, actorId: agentId, status });
    }, "Task status updated");
  }

  // v0.9.8: "Add agent by code" — paste e.g. "a3f7-b2e8" and the API
  // resolves it back to the underlying agent_id / pubkey / DAO source.
  async function onLookupCode(event: FormEvent) {
    event.preventDefault();
    if (!lookupCode.trim()) return;
    setLookupResult("Searching…");
    try {
      const hit = await lookupAgentByCode(lookupCode.trim());
      const where = hit.source === "group" ? `in @${hit.group_slug}` : "in home";
      setLookupResult(`Found ${hit.agent_id} (${hit.code}) ${where}`);
    } catch (e) {
      setLookupResult((e as Error).message);
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Local-first DAO group layer</p>
          <h1>NTH DAO Console</h1>
        </div>
        <div className="status-strip" aria-live="polite">
          <Metric label="Members" value={summary?.members ?? "-"} />
          <Metric label="Channels" value={summary?.channels ?? "-"} />
          <Metric label="Tasks" value={summary?.tasks ?? "-"} />
          <Metric label="Role" value={state?.actor.role ?? "-"} />
        </div>
      </header>

      <section className="workspace">
        <aside className="left-rail">
          <form className="panel" onSubmit={onJoin}>
            <label htmlFor="agent-id">Local agent id</label>
            <div className="inline">
              <input
                id="agent-id"
                value={agentId}
                onChange={(event) => updateAgent(event.target.value)}
                spellCheck={false}
              />
              <button type="submit" disabled={busy}>Join</button>
            </div>
            <p className="hint">
              Current policy: {summary?.team.join_policy ?? "loading"}
              {summary?.actor_code && (
                <> · Your code: <code className="agent-code">{summary.actor_code}</code></>
              )}
            </p>
          </form>

          {/* v0.9.8: Telegram-style "add by code". Paste a friend's a3f7-b2e8
              handle, the API resolves it back to agent_id + pubkey + DAO. */}
          <form className="panel" onSubmit={onLookupCode}>
            <div className="panel-heading">
              <h2>Find by code</h2>
            </div>
            <div className="inline">
              <input
                placeholder="a3f7-b2e8"
                value={lookupCode}
                onChange={(event) => setLookupCode(event.target.value)}
                spellCheck={false}
              />
              <button type="submit" disabled={!lookupCode.trim()}>Find</button>
            </div>
            {lookupResult && <p className="hint">{lookupResult}</p>}
          </form>

          {/* QQ-style "My DAOs" list — one agent ↔ many DAOs. Click to switch. */}
          <section className="panel">
            <div className="panel-heading">
              <h2>My DAOs</h2>
              <span>{daos.length}</span>
            </div>
            <div className="stack dao-list">
              {daos.length === 0 && (
                <p className="empty-inline">Loading DAOs…</p>
              )}
              {daos.map((dao) => {
                const isActive = dao.slug === activeDao;
                const avatar = (dao.display_name || dao.slug).slice(0, 2).toUpperCase();
                return (
                  <button
                    key={dao.slug}
                    type="button"
                    className={`dao-item ${isActive ? "active" : ""} dao-kind-${dao.kind}`}
                    onClick={() => switchDao(dao.slug)}
                    title={dao.description || dao.display_name}
                  >
                    <span className="dao-avatar">{dao.kind === "home" ? "🏠" : avatar}</span>
                    <span className="dao-meta">
                      <span className="dao-name">{dao.display_name}</span>
                      <small>
                        {dao.kind === "home" ? "home" : `@${dao.slug}`}
                        {" · "}{dao.member_count} member{dao.member_count === 1 ? "" : "s"}
                        {dao.joined ? "" : " · not joined"}
                      </small>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          <section className="panel">
            <div className="panel-heading">
              <h2>Channels</h2>
              <span>{state?.channels.length ?? 0}</span>
            </div>
            <div className="stack">
              {state?.channels.map((channel) => (
                <button
                  className={channel.channel_id === selectedChannel ? "channel active" : "channel"}
                  key={channel.channel_id}
                  onClick={() => {
                    setSelectedChannel(channel.channel_id);
                    refresh(agentId, channel.channel_id).catch((error: Error) => setNotice(error.message));
                  }}
                  type="button"
                >
                  <span>#{channel.name}</span>
                  <small>{channel.topic || "No topic"}</small>
                </button>
              ))}
            </div>
          </section>

          <form className="panel" onSubmit={onCreateChannel}>
            <div className="panel-heading">
              <h2>New channel</h2>
            </div>
            <input
              placeholder="Name"
              value={channelName}
              onChange={(event) => setChannelName(event.target.value)}
            />
            <input
              placeholder="Topic"
              value={channelTopic}
              onChange={(event) => setChannelTopic(event.target.value)}
            />
            <button type="submit" disabled={busy || !channelName.trim()}>Create</button>
          </form>
        </aside>

        <section className="conversation">
          <div className="conversation-head">
            <div>
              <p className="eyebrow">
                {state?.dao?.display_name ?? "Channel"}
                {state?.dao?.kind === "group" && <span className="dao-tag">@{state.dao.slug}</span>}
              </p>
              <h2>#{activeChannel?.name ?? (selectedChannel || "general")}</h2>
              {state?.dao?.kind === "group" && state?.channels.length === 0 && (
                <p className="hint">
                  This DAO has no channels yet. Use “New channel” on the left to
                  create one — it will be scoped to <code>@{state.dao.slug}</code>.
                </p>
              )}
            </div>
            <span className="notice">{notice}</span>
          </div>

          <div className="announcement-list">
            {state?.announcements.map((announcement) => (
              <article className="announcement" key={announcement.announcement_id}>
                <strong>{announcement.title}</strong>
                <p>{announcement.body}</p>
                <small>{announcement.author_id} - {shortTime(announcement.created_at)}</small>
              </article>
            ))}
          </div>

          <div className="messages">
            {state?.messages.length ? state.messages.map((message) => (
              <article className="message" key={message.message_id}>
                <div>
                  <strong>{message.sender_id}</strong>
                  <time>{shortTime(message.created_at)}</time>
                </div>
                <p>{message.body}</p>
              </article>
            )) : <p className="empty">No messages in this channel yet.</p>}
          </div>

          <form className="composer" onSubmit={onPostMessage}>
            <textarea
              placeholder="Write a message to this DAO channel"
              value={messageBody}
              onChange={(event) => setMessageBody(event.target.value)}
            />
            <button type="submit" disabled={busy || !messageBody.trim()}>Send</button>
          </form>
        </section>

        <aside className="right-rail">
          <details className="panel collapsible" open>
            <summary className="panel-heading">
              <h2>Members</h2>
              <span className="collapsible-badge">{state?.members.length ?? 0}</span>
            </summary>
            <div className="collapsible-body">
              <div className="member-list">
                {state?.members.map((member) => (
                  <div className="member" key={member.agent_id}>
                    <span>
                      {member.agent_id}
                      {member.code && (
                        <code className="agent-code agent-code-inline">{member.code}</code>
                      )}
                    </span>
                    <small>{member.role}{member.online ? " · online" : ""}</small>
                  </div>
                ))}
              </div>
            </div>
          </details>

          <details className="panel collapsible">
            <summary className="panel-heading">
              <h2>Announcement</h2>
              <span className="collapsible-badge">post</span>
            </summary>
            <form className="collapsible-body" onSubmit={onPostAnnouncement}>
              <input
                placeholder="Title"
                value={announcementTitle}
                onChange={(event) => setAnnouncementTitle(event.target.value)}
              />
              <textarea
                placeholder="Body"
                value={announcementBody}
                onChange={(event) => setAnnouncementBody(event.target.value)}
              />
              <button type="submit" disabled={busy || !announcementTitle.trim()}>Post</button>
            </form>
          </details>

          <details className="panel collapsible">
            <summary className="panel-heading">
              <h2>Tasks</h2>
              <span className="collapsible-badge">{state?.tasks.length ?? 0}</span>
            </summary>
            <div className="collapsible-body">
              <form className="collapsible-subform" onSubmit={onCreateTask}>
                <p className="collapsible-subheading">New task</p>
                <input
                  placeholder="Title"
                  value={taskTitle}
                  onChange={(event) => setTaskTitle(event.target.value)}
                />
                <input
                  placeholder="Assignee id"
                  value={taskAssignee}
                  onChange={(event) => setTaskAssignee(event.target.value)}
                />
                <textarea
                  placeholder="Description"
                  value={taskDescription}
                  onChange={(event) => setTaskDescription(event.target.value)}
                />
                <button type="submit" disabled={busy || !taskTitle.trim()}>Create</button>
              </form>
              <div className="stack">
                {state?.tasks.map((task) => (
                  <article className="task" key={task.task_id}>
                    <strong>{task.title}</strong>
                    <small>{task.assignee_id || "unassigned"} - {task.status}</small>
                    <select
                      value={task.status}
                      onChange={(event) => onUpdateTask(task.task_id, event.target.value as TaskStatus)}
                    >
                      {taskStatuses.map((status) => (
                        <option key={status} value={status}>{status}</option>
                      ))}
                    </select>
                  </article>
                ))}
                {!state?.tasks.length && <p className="empty-inline">No tasks yet.</p>}
              </div>
            </div>
          </details>

          <details className="panel collapsible">
            <summary className="panel-heading">
              <h2>Audit</h2>
              <span className="collapsible-badge">{state?.audit.length ?? 0}</span>
            </summary>
            <div className="collapsible-body">
              <div className="audit-list">
                {state?.audit.slice().reverse().map((event) => (
                  <article className="audit" key={event.event_id}>
                    <strong>{event.event_type}</strong>
                    <span>{event.summary}</span>
                    <small>{event.actor_id} - {shortTime(event.created_at)}</small>
                  </article>
                ))}
                {!state?.audit.length && <p className="empty-inline">No audit events.</p>}
              </div>
            </div>
          </details>

          <details className="panel collapsible contacts-panel-host">
            <summary className="panel-heading">
              <h2>Contacts / Groups</h2>
              <span className="collapsible-badge">
                {wallet ? "signed" : walletError ? "no-key" : "loading"}
              </span>
            </summary>
            <div className="collapsible-body">
              {walletError && (
                <p className="empty-inline">Wallet unavailable: {walletError}</p>
              )}
              <ContactShell
                actorId={agentId}
                actorPubkeyHex={wallet?.pubkeyHex ?? ""}
                sign={wallet?.sign}
              />
            </div>
          </details>
        </aside>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function shortTime(value: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export default App;
