import { type FormEvent, useState } from "react";
import type { DaoSummary, Summary } from "../types";

export interface ChatSidebarProps {
  agentId: string;
  onAgentIdChange: (v: string) => void;
  summary: Summary | null;
  busy: boolean;
  onJoin: () => void;
  daos: DaoSummary[];
  activeDao: string;
  onSwitchDao: (slug: string) => void;
  channels: { channel_id: string; name: string }[];
  selectedChannel: string;
  onSelectChannel: (ch: string) => void;
  onCreateChannel: (name: string, topic: string) => Promise<void>;
}

export function ChatSidebar(props: ChatSidebarProps) {
  const {
    agentId, onAgentIdChange, summary, busy, onJoin,
    daos, activeDao, onSwitchDao,
    channels, selectedChannel, onSelectChannel,
    onCreateChannel,
  } = props;

  const [chName, setChName] = useState("");
  const [chTopic, setChTopic] = useState("");

  async function handleCreateChannel(e: FormEvent) {
    e.preventDefault();
    await onCreateChannel(chName, chTopic);
    setChName(""); setChTopic("");
  }

  function handleJoinClick(e: FormEvent) {
    e.preventDefault();
    onJoin();
  }

  return (
    <>
      {/* Agent ID — collapsed by default */}
      <div className="left-rail-section">
        <details>
          <summary className="left-rail-label" style={{ cursor: "pointer", border: "none", background: "none", margin: 0 }}>
            {agentId}
            {summary?.actor_code && <code className="agent-code" style={{ marginLeft: 6 }}>{summary.actor_code}</code>}
          </summary>
          <form onSubmit={handleJoinClick} style={{ marginTop: 8, display: "grid", gap: 6 }}>
            <input
              value={agentId}
              onChange={e => { onAgentIdChange(e.target.value); localStorage.setItem("nth-dao-agent-id", e.target.value); }}
              placeholder="Agent ID" spellCheck={false}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <button type="submit" disabled={busy} style={{ flex: 1, padding: "6px 12px", fontSize: 12 }}>Join</button>
            </div>
            <p className="hint">{summary?.team.join_policy ?? "…"}</p>
          </form>
        </details>
      </div>

      {/* DAOs */}
      <div className="left-rail-section">
        <div className="left-rail-label">DAOs</div>
        <div className="stack dao-list">
          {!daos.length && <p className="empty-inline">Loading…</p>}
          {daos.map(d => {
            const isActive = d.slug === activeDao;
            const avatar = (d.display_name || d.slug).slice(0, 2).toUpperCase();
            return (
              <button
                key={d.slug}
                className={`dao-item ${isActive ? "active" : ""} dao-kind-${d.kind}`}
                onClick={() => onSwitchDao(d.slug)}
                title={d.description || d.display_name}
              >
                <span className="dao-avatar">{d.kind === "home" ? "🏠" : avatar}</span>
                <span className="dao-meta">
                  <span className="dao-name">{d.display_name}</span>
                  <small>@{d.slug} · {d.member_count}m</small>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Channels */}
      <div className="left-rail-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="left-rail-label">Channels</div>
        <div className="stack">
          {channels.map(c => (
            <button
              key={c.channel_id}
              className={`channel ${c.channel_id === selectedChannel ? "active" : ""}`}
              onClick={() => onSelectChannel(c.channel_id)}
            >
              <span># {c.name}</span>
            </button>
          ))}
        </div>
      </div>

      {/* + New Channel */}
      <div className="left-rail-section">
        <details>
          <summary className="left-rail-label" style={{ cursor: "pointer", border: "none", background: "none", margin: 0 }}>
            + New Channel
          </summary>
          <form onSubmit={handleCreateChannel} style={{ marginTop: 6, display: "grid", gap: 6 }}>
            <input placeholder="Name" value={chName} onChange={e => setChName(e.target.value)} />
            <input placeholder="Topic" value={chTopic} onChange={e => setChTopic(e.target.value)} />
            <button type="submit" disabled={busy || !chName.trim()} style={{ padding: "6px 12px", fontSize: 12 }}>
              Create
            </button>
          </form>
        </details>
      </div>
    </>
  );
}
