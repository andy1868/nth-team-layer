# QQ-style contact + nearby + groups + governance panels (v0.9.6)

Drop-in React components that consume the v0.9.6 web APIs:

- `ContactsPanel` — fuzzy-search registered agents; add by exact ID or DID.
- `NearbyPanel`   — UDP LAN discovery, with optional pre-shared key.
- `GroupsPanel`   — list / search / create workspace-unique groups.
- `GovernancePanel` — propose & vote on policy changes for one group.

The `ContactShell` wrapper at `./index.tsx` glues all four together with a
top tab bar.

## Integration

In `App.tsx`:

```tsx
import { ContactShell } from "./panels";

function App() {
  // Your existing wallet wiring lives here. ContactShell takes:
  //   actorId         — the agent_id you've already established
  //   actorPubkeyHex  — the Ed25519 pubkey (hex) for signing requests
  //   sign(payload)   — wallet signs canonical JSON, returns hex sig
  const { actorId, actorPubkeyHex, sign } = useWallet();

  return (
    <main>
      <ContactShell
        actorId={actorId}
        actorPubkeyHex={actorPubkeyHex}
        sign={sign}
      />
    </main>
  );
}
```

When you don't have a wallet wired up yet, omit `actorPubkeyHex` / `sign` —
the panels render in browse-only mode.

## Wire-format expectations

- **Sign over canonical JSON.** Every `sign(payload)` call must produce a
  hex-encoded Ed25519 signature over `canonical_json(payload)`. The Python
  `nth_dao.identity.canonical_json` is the reference; a 30-line port that
  uses `JSON.stringify` with sorted keys + UTF-8 encoding matches it.
- **No private keys in the URL.** All `pubkey_hex` fields are 64-character
  hex strings (Ed25519 32-byte pubkeys). Never accept a private key from
  HTTP.

## Build

This module is plain TS/TSX. After editing, rebuild with:

```bash
cd frontend
npm run build
```

The Vite output ends up in `frontend/dist`; the existing `nth_dao/web`
serves the build out of `nth_dao/web/static/`. Wire the build pipeline
of your choice; the panels themselves have no opinion about it.
