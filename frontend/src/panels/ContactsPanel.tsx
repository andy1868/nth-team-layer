

import { useState } from "react";
import {
  type AgentMatch,
  addAgent,
  searchAgents
} from "../contacts";

interface Props {
  actorId: string;
}

export function ContactsPanel({ actorId }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<AgentMatch[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [directId, setDirectId] = useState("");
  const [directDid, setDirectDid] = useState("");

  async function runSearch() {
    setLoading(true);
    setMessage(null);
    try {
      const hits = await searchAgents(query, 20);
      setResults(hits);
      if (hits.length === 0) setMessage("No matches.");
    } catch (e) {
      setMessage(`Search failed: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function add(target: { agent_id?: string; did?: string; label?: string }) {
    setMessage(null);
    try {
      await addAgent({
        actorId,
        targetAgentId: target.agent_id ?? "",
        targetDid: target.did ?? "",
        label: target.label ?? ""
      });
      setMessage(`Added ${target.agent_id ?? target.did}`);
    } catch (e) {
      setMessage(`Add failed: ${(e as Error).message}`);
    }
  }

  return (
    <section className="nth-panel">
      <h2>Find / Add Contact</h2>

      <div className="nth-search-bar">
        <input
          type="search"
          placeholder="Search by name, label, capability…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <button onClick={runSearch} disabled={loading}>
          {loading ? "…" : "Search"}
        </button>
      </div>

      {message && <p className="nth-flash">{message}</p>}

      <ul className="nth-result-list">
        {results.map((r) => (
          <li key={r.agent_id} className="nth-result">
            <div>
              <strong>{r.agent_id}</strong>
              <span className={`nth-status nth-status-${r.status}`}>{r.status}</span>
              {r.capabilities.length > 0 && (
                <small> · {r.capabilities.slice(0, 3).join(", ")}</small>
              )}
            </div>
            <button onClick={() => add({ agent_id: r.agent_id })}>+ Add</button>
          </li>
        ))}
      </ul>

      <div className="nth-direct-add">
        <h3>Or add by exact ID / DID</h3>
        <input
          placeholder="agent_id"
          value={directId}
          onChange={(e) => setDirectId(e.target.value)}
        />
        <input
          placeholder="did:key:z6Mk…"
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
