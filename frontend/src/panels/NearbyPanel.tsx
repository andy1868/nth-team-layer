// QQ-style "People Nearby" panel — UDP LAN discovery.

import { useState } from "react";
import { type LANPeer, addAgent, discoverLanPeers } from "../contacts";

interface Props {
  actorId: string;
}

export function NearbyPanel({ actorId }: Props) {
  const [peers, setPeers] = useState<LANPeer[]>([]);
  const [scanning, setScanning] = useState(false);
  const [psk, setPsk] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function scan() {
    setScanning(true);
    setError(null);
    try {
      const hits = await discoverLanPeers({
        timeoutSeconds: 3,
        psk: psk || undefined
      });
      setPeers(hits);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setScanning(false);
    }
  }

  async function invite(peer: LANPeer) {
    try {
      await addAgent({
        actorId,
        targetAgentId: peer.agent_id,
        label: peer.label
      });
    } catch (e) {
      setError(`Invite failed: ${(e as Error).message}`);
    }
  }

  return (
    <section className="qq-panel">
      <h2>People Nearby (LAN)</h2>

      <div className="qq-nearby-controls">
        <label>
          PSK (optional):
          <input
            type="password"
            placeholder="leave blank for open mode"
            value={psk}
            onChange={(e) => setPsk(e.target.value)}
          />
        </label>
        <button onClick={scan} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan LAN"}
        </button>
      </div>

      {error && <p className="qq-flash qq-error">{error}</p>}

      {peers.length === 0 && !scanning && (
        <p className="qq-empty">
          No peers seen yet. Click <em>Scan LAN</em>. Your team needs nth-dao agents
          listening on UDP/9877 on the same broadcast domain.
        </p>
      )}

      <ul className="qq-result-list">
        {peers.map((p) => (
          <li key={p.agent_id} className="qq-result">
            <div>
              <strong>{p.label || p.agent_id}</strong>
              <small> · {p.source_addr} · {p.rtt_ms.toFixed(0)}ms</small>
              {p.capabilities.length > 0 && (
                <div className="qq-caps">{p.capabilities.join(", ")}</div>
              )}
              {p.pubkey_hex && (
                <code className="qq-did">{p.pubkey_hex.slice(0, 16)}…</code>
              )}
            </div>
            <button onClick={() => invite(p)}>+ Add</button>
          </li>
        ))}
      </ul>
    </section>
  );
}
