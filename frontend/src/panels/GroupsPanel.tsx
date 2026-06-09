

import { useEffect, useState } from "react";
import {
  type UniqueGroup,
  listGroups,
  prepareGroup,
  publishGroup,
  searchGroups
} from "../contacts";

interface Props {
  actorId: string;
  /** Hex-encoded Ed25519 pubkey of the signing wallet (browser keystore). */
  actorPubkeyHex: string;
  /**
   * Sign a canonical JSON object with the actor's private key (browser wallet).
   * Returns the signature as a hex string. The host app supplies this so the
   * UI never sees raw keys.
   */
  sign: (obj: unknown) => Promise<string>;
  onSelect?: (group: UniqueGroup) => void;
}

export function GroupsPanel({ actorId, actorPubkeyHex, sign, onSelect }: Props) {
  const [groups, setGroups] = useState<UniqueGroup[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    display_name: "",
    description: "",
    policy: "open" as "open" | "approval" | "closed" | "voted"
  });

  async function refresh() {
    try {
      setGroups(await listGroups());
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function runSearch() {
    if (!query.trim()) return refresh();
    try {
      setGroups(await searchGroups(query, undefined, 20));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function createGroup() {
    setError(null);
    setCreating(true);
    try {
      const prep = await prepareGroup({
        actorId,
        actorPubkeyHex,
        displayName: form.display_name,
        description: form.description,
        policy: form.policy
      });
      const record = { ...prep.unsigned_record };
      // The client must populate group_id and sig before publishing.
      record.group_id = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      record.sig = await sign({ ...prep.to_sign, group_id: record.group_id });
      await publishGroup(record);
      setForm({ display_name: "", description: "", policy: "open" });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setCreating(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <section className="nth-panel">
      <h2>Groups / DAOs</h2>

      <div className="nth-search-bar">
        <input
          type="search"
          placeholder="Search by name, slug, description…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <button onClick={runSearch}>Search</button>
        <button onClick={refresh}>All</button>
      </div>

      {error && <p className="nth-flash nth-error">{error}</p>}

      <ul className="nth-group-list">
        {groups.map((g) => (
          <li
            key={g.group_id}
            className={`nth-group nth-policy-${g.policy}`}
            onClick={() => onSelect?.(g)}
          >
            <div>
              <strong>{g.display_name}</strong>{" "}
              <code className="nth-slug">@{g.slug}</code>
              <small className="nth-policy-badge">{g.policy}</small>
            </div>
            <div className="nth-group-meta">
              {g.member_pubkeys.length} member{g.member_pubkeys.length === 1 ? "" : "s"}
              {g.description && ` · ${g.description.slice(0, 60)}`}
            </div>
          </li>
        ))}
      </ul>

      <details className="nth-create-group">
        <summary>+ Create a new group</summary>
        <div className="nth-form">
          <input
            placeholder="Display name (e.g., Privacy Working Group)"
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          />
          <input
            placeholder="Short description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
          <select
            value={form.policy}
            onChange={(e) =>
              setForm({
                ...form,
                policy: e.target.value as typeof form.policy
              })
            }
          >
            <option value="open">Open — anyone can join</option>
            <option value="approval">Approval — admin admits</option>
            <option value="closed">Closed — invite only</option>
            <option value="voted">Voted — quorum of members admits</option>
          </select>
          <button onClick={createGroup} disabled={!form.display_name || creating}>
            {creating ? "Creating…" : "Sign + Publish"}
          </button>
        </div>
      </details>
    </section>
  );
}
