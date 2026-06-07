import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { getDaoState } from "./api";
import type { DaoState, Summary } from "./types";

vi.mock("./crypto", () => ({
  loadOrCreateWallet: vi.fn().mockResolvedValue({
    pubkeyHex: "00".repeat(32),
    sign: vi.fn()
  })
}));

vi.mock("./panels", () => ({
  ContactShell: () => <div data-testid="contact-shell" />
}));

vi.mock("./api", () => ({
  createChannel: vi.fn(),
  createTask: vi.fn(),
  getDaos: vi.fn().mockResolvedValue({ daos: [] }),
  getSummary: vi.fn(),
  // Week-1 Task 5: App.tsx now fetches build_id at mount. Default
  // mock returns a placeholder so the useEffect resolves cleanly.
  getBuildId: vi.fn().mockResolvedValue({
    backend_git: "test",
    backend_started_at: "2026-06-07T00:00:00",
    now: "2026-06-07T00:00:00"
  }),
  // DID bootstrap (2026-06-07): the test mock returns a stable
  // placeholder so the App component's top-bar effect resolves.
  getIdentity: vi.fn().mockResolvedValue({
    agent_id: "admin",
    did: "did:key:z6MkTest",
    pubkey_hex: "00".repeat(32),
    pubkey_prefix: "0000000000000000",
    code: "0000-0000",
    bootstrap_error: ""
  }),
  join: vi.fn(),
  // Architect R-13 (2026-06-07): lookupAgentByCode is no longer
  // exported from api.ts; the mock entry was removed with the function.
  postAnnouncement: vi.fn(),
  postMessage: vi.fn(),
  updateTaskStatus: vi.fn(),
  getDaoState: vi.fn()
}));

const summary: Summary = {
  team: {
    team_name: "NTH DAO",
    join_policy: "open",
    member_ids: ["admin"],
    admin_ids: ["admin"],
    roles: { admin: "owner" }
  },
  workspace: "local-workspace",
  workspace_is_local: true,
  members: 1,
  channels: 2,
  tasks: 0,
  online_agents: 0,
  active_missions: 0,
  blackboard_entries: 0,
  server_time: "2026-06-07T00:00:00Z",
  actor_code: "8c69-76e5"
};

const daoState: DaoState = {
  team: summary.team,
  actor: { agent_id: "admin", role: "owner" },
  members: [{ agent_id: "admin", role: "owner", online: false }],
  channels: [
    {
      channel_id: "general",
      name: "general",
      topic: "General",
      is_private: false,
      member_ids: [],
      created_by: "admin",
      created_at: "2026-06-07T00:00:00Z"
    },
    {
      channel_id: "debug",
      name: "debug",
      topic: "Debug",
      is_private: false,
      member_ids: [],
      created_by: "admin",
      created_at: "2026-06-07T00:00:00Z"
    }
  ],
  messages: [],
  announcements: [],
  tasks: [],
  audit: [],
  active_channel_id: "general"
};

describe("App channel polling", () => {
  beforeEach(async () => {
    window.localStorage.clear();
    const api = await import("./api");
    vi.mocked(api.getSummary).mockResolvedValue(summary);
    vi.mocked(api.getDaoState).mockResolvedValue(daoState);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("keeps polling the selected channel instead of falling back to general", async () => {
    render(<App />);

    await screen.findByRole("button", { name: /#debug/i });
    fireEvent.click(screen.getByRole("button", { name: /#debug/i }));

    await waitFor(() => {
      expect(getDaoState).toHaveBeenCalledWith("home", "admin", "debug");
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5200));
    });

    const calls = vi.mocked(getDaoState).mock.calls;
    expect(calls[calls.length - 1]).toEqual(["home", "admin", "debug"]);
  }, 10000);
});
