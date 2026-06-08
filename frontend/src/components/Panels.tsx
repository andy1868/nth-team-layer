import { type FormEvent, useEffect, useState } from "react";
import type { DaoState, TaskStatus } from "../types";
import { shortTime, taskStatuses } from "./utils";

/* ──────────────────────── ChatArea ──────────────────────── */

export interface ChatAreaProps {
  state: DaoState | null;
  notice: string;
  busy: boolean;
  selectedChannel: string;
  onSend: (body: string) => Promise<void>;
}

export function ChatArea({ state, notice, busy, selectedChannel, onSend }: ChatAreaProps) {
  const [body, setBody] = useState("");
  const [announceExpanded, setAnnounceExpanded] = useState(false);
  const [announceDismissed, setAnnounceDismissed] = useState<string | null>(null);
  const activeChan = state?.channels.find(c => c.channel_id === selectedChannel);

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!body.trim()) return;
    await onSend(body);
    setBody("");
  }

  // Latest announcement (for QQ-style pinned banner)
  const latestAnnounce = state?.announcements?.[state.announcements.length - 1];
  const latestId = latestAnnounce?.announcement_id ?? null;

  // Reset dismiss/expand state on channel switch (Issue #2)
  useEffect(() => {
    setAnnounceDismissed(null);
    setAnnounceExpanded(false);
  }, [selectedChannel]);

  return (
    <section className="conversation">
      {/* QQ-style pinned announcement banner — above everything */}
      {latestAnnounce && announceDismissed !== latestId ? (
        <div className={`announce-banner ${announceExpanded ? "expanded" : ""}`}>
          <div
            className="announce-banner-bar"
            onClick={() => setAnnounceExpanded(!announceExpanded)}
          >
            <span className="announce-banner-icon">📢</span>
            <span className="announce-banner-label">公告</span>
            <span className="announce-banner-title">
              {latestAnnounce.title}
            </span>
            <span className="announce-banner-arrow">
              {announceExpanded ? "▲" : "▼"}
            </span>
            <button
              className="announce-banner-close"
              onClick={(e) => { e.stopPropagation(); setAnnounceDismissed(latestId); setAnnounceExpanded(false); }}
              title="关闭"
            >
              ✕
            </button>
          </div>
          {announceExpanded && (
            <div className="announce-banner-body">
              <p>{latestAnnounce.body}</p>
              <small>{latestAnnounce.author_id} · {shortTime(latestAnnounce.created_at)}</small>
            </div>
          )}
        </div>
      ) : null}

      <div className="conversation-head">
        <div>
          <p className="eyebrow">
            {state?.dao?.display_name ?? "Home"}
            {state?.dao?.kind === "group" && <span className="dao-tag">@{state.dao.slug}</span>}
          </p>
          <h2># {activeChan?.name ?? (selectedChannel || "general")}</h2>
        </div>
        <span className="notice">{notice}</span>
      </div>

      <div className="messages">
        {state?.messages.length ? state.messages.map(m => (
          <article className="message" key={m.message_id}>
            <div><strong>{m.sender_id}</strong><time>{shortTime(m.created_at)}</time></div>
            <p>{m.body}</p>
          </article>
        )) : <p className="empty">No messages yet. Start a conversation.</p>}
      </div>

      <form className="composer" onSubmit={handleSend}>
        <textarea
          placeholder={`Message #${activeChan?.name ?? "general"}`}
          value={body}
          onChange={e => setBody(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (body.trim() && !busy) handleSend(e);
            }
          }}
        />
        <button type="submit" disabled={busy || !body.trim()}>Send</button>
      </form>
    </section>
  );
}

/* ──────────────────────── PanelTasks ──────────────────────── */

export interface PanelTasksProps {
  tasks: { task_id: string; title: string; assignee_id?: string; status: string }[];
  busy: boolean;
  onCreateTask: (title: string, desc: string, assignee: string) => Promise<void>;
  onUpdateTask: (taskId: string, status: TaskStatus) => void;
}

export function PanelTasks({ tasks, busy, onCreateTask, onUpdateTask }: PanelTasksProps) {
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [assignee, setAssignee] = useState("");

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    await onCreateTask(title, desc, assignee);
    setTitle(""); setDesc(""); setAssignee("");
  }

  return (
    <>
      <div className="right-rail-header">
        <h2>✅ Tasks</h2>
        <span className="count">{tasks.length}</span>
      </div>
      <div className="right-rail-body">
        <div className="form-section" style={{ marginBottom: 12 }}>
          <div className="form-section-label">New Task</div>
          <form className="right-rail-form" onSubmit={handleCreate}>
            <input placeholder="Title" value={title} onChange={e => setTitle(e.target.value)} />
            <input placeholder="Assignee" value={assignee} onChange={e => setAssignee(e.target.value)} />
            <textarea placeholder="Description" value={desc} onChange={e => setDesc(e.target.value)} />
            <button className="btn-primary" type="submit" disabled={busy || !title.trim()}>Create Task</button>
          </form>
        </div>
        <div className="task-list">
          {tasks.map(t => (
            <div className="task-card" key={t.task_id}>
              <h4>{t.title}</h4>
              <div className="task-meta">
                <span>{t.assignee_id || "unassigned"}</span>
                <span className={`task-status ${t.status}`}>{t.status}</span>
              </div>
              <select value={t.status} onChange={e => onUpdateTask(t.task_id, e.target.value as TaskStatus)}>
                {taskStatuses.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          ))}
          {!tasks.length && (
            <div className="empty-state">
              <span className="empty-icon">📋</span><p>No tasks yet</p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ──────────────────────── PanelAnnounce ──────────────────────── */

export interface PanelAnnounceProps {
  busy: boolean;
  onPost: (title: string, body: string) => Promise<void>;
  announcements: { announcement_id: string; title: string; body: string; author_id: string; created_at: string }[];
}

export function PanelAnnounce({ busy, onPost, announcements }: PanelAnnounceProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  async function handlePost(e: FormEvent) {
    e.preventDefault();
    await onPost(title, body);
    setTitle(""); setBody("");
  }

  return (
    <>
      <div className="right-rail-header">
        <h2>📢 Announce</h2>
        <span className="count">{announcements.length}</span>
      </div>
      <div className="right-rail-body">
        <div className="form-section" style={{ marginBottom: 12 }}>
          <div className="form-section-label">Post</div>
          <form className="right-rail-form" onSubmit={handlePost}>
            <input placeholder="Title" value={title} onChange={e => setTitle(e.target.value)} />
            <textarea placeholder="Body" value={body} onChange={e => setBody(e.target.value)} />
            <button className="btn-primary" type="submit" disabled={busy || !title.trim()}>Post</button>
          </form>
        </div>
        <div className="task-list">
          {announcements.map(a => (
            <div className="announce-card" key={a.announcement_id}>
              <h4>{a.title}</h4>
              <p>{a.body}</p>
              <small>{a.author_id} · {shortTime(a.created_at)}</small>
            </div>
          ))}
          {!announcements.length && (
            <div className="empty-state">
              <span className="empty-icon">📢</span><p>No announcements</p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ──────────────────────── PanelAudit ──────────────────────── */

export interface PanelAuditProps {
  audit: { event_id: string; event_type: string; summary: string; actor_id: string; created_at: string }[];
}

export function PanelAudit({ audit }: PanelAuditProps) {
  return (
    <>
      <div className="right-rail-header">
        <h2>📜 Audit</h2>
        <span className="count">{audit.length}</span>
      </div>
      <div className="right-rail-body">
        <div className="audit-list">
          {audit.slice().reverse().map(ev => (
            <div className="audit-entry" key={ev.event_id}>
              <div className="audit-type">{ev.event_type}</div>
              <div className="audit-summary">{ev.summary}</div>
              <div className="audit-time">{ev.actor_id} · {shortTime(ev.created_at)}</div>
            </div>
          ))}
          {!audit.length && (
            <div className="empty-state">
              <span className="empty-icon">📜</span><p>No events</p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ──────────────────────── EmptyPanel ──────────────────────── */

export function EmptyPanel() {
  return (
    <div className="empty-state">
      <span className="empty-icon">◈</span>
      <p>Select a tab</p>
      <p style={{ fontSize: 12, marginTop: 4 }}>Use the icons on the left</p>
    </div>
  );
}
