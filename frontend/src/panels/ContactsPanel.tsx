// Contact discovery panel - one search box that covers home members,
// LAN registry, and group registry pubkeys.

import { useEffect, useState } from "react";
import {
  type AgentMatch,
  addAgent,
  searchAgents
} from "../contacts";

interface Props {
  actorId: string;
}

interface RecentlyAdded {
  agent_id: string;
  did: string;
  label: string;
  added_at: string;
}

const RECENTLY_ADDED_KEY_PREFIX = "nth-dao-recently-added:";
const RECENTLY_ADDED_MAX = 8;

function recentlyAddedKey(actorId: string): string {
  const safe = (actorId || "").trim();
  return RECENTLY_ADDED_KEY_PREFIX + (safe || "__anon__");
}

function isRecentlyAddedEntry(value: unknown): value is RecentlyAdded {
  return (
    !!value
    && typeof value === "object"
    && typeof (value as RecentlyAdded).agent_id === "string"
    && typeof (value as RecentlyAdded).did === "string"
    && typeof (value as RecentlyAdded).label === "string"
    && typeof (value as RecentlyAdded).added_at === "string"
  );
}

function loadRecentlyAdded(actorId: string): RecentlyAdded[] {
  try {
    const raw = window.localStorage.getItem(recentlyAddedKey(actorId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isRecentlyAddedEntry)
      .slice(0, RECENTLY_ADDED_MAX);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[ContactsPanel] Recently Added load failed:", err);
    return [];
  }
}

function saveRecentlyAdded(actorId: string, list: RecentlyAdded[]): void {
  try {
    window.localStorage.setItem(
      recentlyAddedKey(actorId),
      JSON.stringify(list.slice(0, RECENTLY_ADDED_MAX))
    );
  } catch {
    // localStorage may be unavailable in private mode or under quota.
  }
}

function shortTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const ms = Date.now() - d.getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function sourceBadge(source: string | undefined): string {
  switch (source) {
    case "home": return "team";
    case "registry": return "live";
    case "group": return "group";
    default: return source ?? "";
  }
}

function canonicalHandle(value: { agent_id: string; did: string }): string {
  return (value.did || value.agent_id).toLowerCase().trim();
}

export function ContactsPanel({ actorId }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<AgentMatch[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [directId, setDirectId] = useState("");
  const [directDid, setDirectDid] = useState("");
  const [recent, setRecent] = useState<RecentlyAdded[]>([]);

  useEffect(() => {
    setRecent(loadRecentlyAdded(actorId));
  }, [actorId]);

  async function runSearch() {
    setLoading(true);
    setMessage(null);
    try {
      const hits = await searchAgents(query, 20, actorId);
      setResults(hits);
      if (hits.length === 0) setMessage("No matches.");
    } catch (e) {
      setMessage(`Search failed: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function add(target: {
    agent_id?: string;
    did?: string;
    label?: string;
  }) {
    setMessage(null);
    try {
      await addAgent({
        actorId,
        targetAgentId: target.agent_id ?? "",
        targetDid: target.did ?? "",
        label: target.label ?? ""
      });
      const handle = target.agent_id ?? target.did ?? "";
      setMessage(`Added ${handle}`);

      const newEntry: RecentlyAdded = {
        agent_id: target.agent_id ?? "",
        did: target.did ?? "",
        label: target.label ?? "",
        added_at: new Date().toISOString()
      };
      const newHandle = canonicalHandle(newEntry);
      const next: RecentlyAdded[] = [
        newEntry,
        ...recent.filter((r) => canonicalHandle(r) !== newHandle)
      ].slice(0, RECENTLY_ADDED_MAX);
      setRecent(next);
      saveRecentlyAdded(actorId, next);
    } catch (e) {
      setMessage(`Add failed: ${(e as Error).message}`);
    }
  }

  return (
    <section className="chat-panel">
      <h2>Find / Add Contact</h2>

      <div className="chat-search-bar">
        <input
          type="search"
          placeholder="Search by name, code, capability, or DID"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <button onClick={runSearch} disabled={loading}>
          {loading ? "Searching..." : "Search"}
        </button>
      </div>

      {message && <p className="chat-flash">{message}</p>}

      {recent.length > 0 && (
        <div className="chat-recent">
          <h3>Recently Added</h3>
          <ul className="chat-recent-list">
            {recent.map((r) => (
              <li key={`${r.agent_id}|${r.did}|${r.added_at}`} className="chat-recent-row">
                <strong>{r.agent_id || r.did}</strong>
                {r.label && <span className="chat-recent-label">({r.label})</span>}
                <small>{shortTime(r.added_at)}</small>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ul className="chat-result-list">
        {results.map((r) => (
          <li key={`${r.source ?? ""}|${r.agent_id}`} className="chat-result">
            <div className="chat-result-main">
              <strong>{r.agent_id}</strong>
              {r.code && <code className="chat-result-code">{r.code}</code>}
              {r.source && (
                <span className={`chat-result-source chat-result-source-${r.source}`}>
                  {sourceBadge(r.source)}
                </span>
              )}
              {r.role && <span className="chat-result-role">{r.role}</span>}
              <span className={`chat-status chat-status-${r.status}`}>
                {r.status}
              </span>
            </div>
            <div className="chat-result-meta">
              {r.capabilities.length > 0 && (
                <small>
                  caps: {r.capabilities.slice(0, 3).join(", ")}
                  {r.capabilities.length > 3 ? ` +${r.capabilities.length - 3}` : ""}
                </small>
              )}
              {typeof r.endorsement_count === "number" && r.endorsement_count > 0 && (
                <small className="chat-result-endorsements">
                  trust: {r.endorsement_count} endorsement
                  {r.endorsement_count === 1 ? "" : "s"}
                </small>
              )}
            </div>
            <button onClick={() => add({ agent_id: r.agent_id })}>+ Add</button>
          </li>
        ))}
      </ul>

      <div className="chat-direct-add">
        <h3>Or add by exact ID / DID</h3>
        <input
          placeholder="agent_id"
          value={directId}
          onChange={(e) => setDirectId(e.target.value)}
        />
        <input
          placeholder="did:key:z6Mk..."
          value={directDid}
          onChange={(e) => setDirectDid(e.target.value)}
        />
        <button
          onClick={() =>
            add({ agent_id: directId, did: directDid }).then(() => {
              setDirectId("");
              setDirectDid("");
            })
          }
          disabled={!directId && !directDid}
        >
          Add
        </button>
      </div>
    </section>
  );
}
