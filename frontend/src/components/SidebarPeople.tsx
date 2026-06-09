import { type FormEvent, useState } from "react";
import type { BrowserWallet } from "../crypto";
import type { Member } from "../types";
import { ContactShell } from "../panels";

export interface PeopleSidebarProps {
  members: Member[];
  agentId: string;
  busy: boolean;
  wallet: BrowserWallet | null;
  walletError: string | null;
  onLookupAgent: (code: string) => Promise<void>;
}

export function PeopleSidebar(props: PeopleSidebarProps) {
  const { members, agentId, busy, wallet, walletError, onLookupAgent } = props;

  const [lookupCode, setLookupCode] = useState("");
  const [lookupResult, setLookupResult] = useState("");

  async function handleLookup(e: FormEvent) {
    e.preventDefault();
    if (!lookupCode.trim()) return;
    setLookupResult("Searching…");
    try {
      await onLookupAgent(lookupCode.trim());
      setLookupResult("Found");
    } catch (err) {
      setLookupResult((err as Error).message);
    }
  }

  return (
    <>
      {/* Members in current DAO */}
      <div className="left-rail-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="left-rail-label">Members</div>
        <div className="member-list">
          {members.map(m => (
            <div className="member" key={m.agent_id}>
              <div className="member-avatar">{m.agent_id.slice(0, 2).toUpperCase()}</div>
              <div className="member-info">
                <div className="member-name">
                  {m.agent_id}
                  {m.code && <code className="agent-code agent-code-inline">{m.code}</code>}
                </div>
                <div className="member-meta">{m.role}{m.online ? " · online" : ""}</div>
              </div>
              <div className={`member-status ${m.online ? "" : "offline"}`} />
            </div>
          ))}
        </div>
      </div>

      {/* Find / Add agent */}
      <div className="left-rail-section">
        <div className="left-rail-label">Find / Add</div>
        <form onSubmit={handleLookup} style={{ display: "flex", gap: 6 }}>
          <input
            placeholder="Agent code (a3f7-b2e8)"
            value={lookupCode}
            onChange={e => setLookupCode(e.target.value)}
            spellCheck={false}
            style={{ flex: 1 }}
          />
          <button type="submit" disabled={!lookupCode.trim() || busy} style={{ padding: "6px 10px", fontSize: 12 }}>
            Find
          </button>
        </form>
        {lookupResult && <p className="hint" style={{ marginTop: 6 }}>{lookupResult}</p>}
      </div>

      {/* Contacts / Groups */}
      <div className="left-rail-section" style={{ flex: 1, overflow: "auto" }}>
        <div className="left-rail-label">Contacts & Groups</div>
        {walletError && <p className="empty-inline">Wallet: {walletError}</p>}
        <div className="contacts-panel">
          <ContactShell actorId={agentId} actorPubkeyHex={wallet?.pubkeyHex ?? ""} sign={wallet?.sign} />
        </div>
      </div>
    </>
  );
}
