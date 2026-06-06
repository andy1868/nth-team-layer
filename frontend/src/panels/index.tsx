// QQ-style panel shell. Drop into App.tsx as <ContactShell ... /> to get the
// 5 sidebar tabs: Contacts · Nearby · Groups · Governance · Mandates.

import { useState } from "react";
import type { UniqueGroup } from "../contacts";
import { ContactsPanel } from "./ContactsPanel";
import { GovernancePanel } from "./GovernancePanel";
import { GroupsPanel } from "./GroupsPanel";
import { MandatesPanel } from "./MandatesPanel";
import { NearbyPanel } from "./NearbyPanel";
import "./qq-style.css";

type Tab = "contacts" | "nearby" | "groups" | "governance" | "mandates";

interface Props {
  actorId: string;
  /**
   * Hex-encoded Ed25519 pubkey of the signing wallet. Until the browser
   * keystore lands, you can supply a stub from the host app and use
   * read-only operations only.
   */
  actorPubkeyHex?: string;
  /**
   * `sign(payload)` produces a hex-encoded Ed25519 signature over the
   * canonical JSON of `payload`. Supplied by the host app (browser wallet
   * extension or a server-side surrogate). When omitted, the panels still
   * render but signing-required actions disable.
   */
  sign?: (obj: unknown) => Promise<string>;
}

export function ContactShell({ actorId, actorPubkeyHex = "", sign }: Props) {
  const [tab, setTab] = useState<Tab>("contacts");
  const [selectedGroup, setSelectedGroup] = useState<UniqueGroup | null>(null);

  const noSignWarning = !sign && (tab === "groups" || tab === "governance") && (
    <p className="qq-flash">
      Signing wallet not connected. Browse-only mode — creating groups and
      voting are disabled.
    </p>
  );

  return (
    <div className="qq-shell">
      <nav className="qq-tab-bar">
        <button
          className={`qq-tab ${tab === "contacts" ? "active" : ""}`}
          onClick={() => setTab("contacts")}
        >
          Contacts
        </button>
        <button
          className={`qq-tab ${tab === "nearby" ? "active" : ""}`}
          onClick={() => setTab("nearby")}
        >
          Nearby
        </button>
        <button
          className={`qq-tab ${tab === "groups" ? "active" : ""}`}
          onClick={() => setTab("groups")}
        >
          Groups
        </button>
        <button
          className={`qq-tab ${tab === "governance" ? "active" : ""}`}
          onClick={() => setTab("governance")}
          disabled={!selectedGroup}
        >
          Governance{selectedGroup ? ` (@${selectedGroup.slug})` : ""}
        </button>
        <button
          className={`qq-tab ${tab === "mandates" ? "active" : ""}`}
          onClick={() => setTab("mandates")}
        >
          Mandates
        </button>
      </nav>

      {noSignWarning}

      {tab === "contacts" && <ContactsPanel actorId={actorId} />}
      {tab === "nearby" && <NearbyPanel actorId={actorId} />}
      {tab === "groups" && sign && actorPubkeyHex && (
        <GroupsPanel
          actorId={actorId}
          actorPubkeyHex={actorPubkeyHex}
          sign={sign}
          onSelect={(g) => {
            setSelectedGroup(g);
            setTab("governance");
          }}
        />
      )}
      {tab === "groups" && (!sign || !actorPubkeyHex) && (
        <GroupsPanel
          actorId={actorId}
          actorPubkeyHex={actorPubkeyHex}
          // The shell never calls sign() in browse mode but TS needs a stub.
          sign={async () => {
            throw new Error("wallet not connected");
          }}
        />
      )}
      {tab === "governance" && selectedGroup && sign && actorPubkeyHex && (
        <GovernancePanel
          group={selectedGroup}
          actorPubkeyHex={actorPubkeyHex}
          sign={sign}
        />
      )}
      {tab === "mandates" && (
        <MandatesPanel
          actorId={actorId}
          walletDid={actorPubkeyHex ? `did:key:${actorPubkeyHex}` : undefined}
        />
      )}
    </div>
  );
}

export { ContactsPanel } from "./ContactsPanel";
export { GovernancePanel } from "./GovernancePanel";
export { GroupsPanel } from "./GroupsPanel";
export { MandatesPanel } from "./MandatesPanel";
export { NearbyPanel } from "./NearbyPanel";
