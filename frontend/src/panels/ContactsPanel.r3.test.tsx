// Architect R-3 + R-10 (2026-06-07): Recently Added per-actor scoping
// + malformed-storage hardening.
//
// Pins:
//   R-3a - localStorage key is scoped by actorId, so switching actor
//          in the top bar swaps the rail (no cross-actor leak)
//   R-3b - swapping actorId mid-mount triggers a reload from the new
//          actor's bucket
//   R-3c - the legacy single-bucket key is no longer read
//   R-10 - tampered / mistyped localStorage entries are filtered out
//          rather than rendered (which would TypeError on r.agent_id)

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor
} from "@testing-library/react";

import { ContactsPanel } from "./ContactsPanel";
import * as contactsModule from "../contacts";

beforeEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

const validEntry = {
  agent_id: "alice",
  did: "",
  label: "the auditor",
  added_at: "2026-06-07T12:00:00Z",
};


// ===== R-3: per-actor scoping =====


describe("ContactsPanel - Recently Added per-actor scoping (R-3)", () => {
  it("admin sees their own bucket, not the alice bucket", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([validEntry])
    );
    window.localStorage.setItem(
      "nth-dao-recently-added:alice",
      JSON.stringify([{ ...validEntry, agent_id: "bob-secret" }])
    );

    render(<ContactsPanel actorId="admin" />);
    expect(screen.getByText("alice")).toBeTruthy();
    expect(screen.queryByText("bob-secret")).toBeNull();
  });

  it("alice sees their own bucket, not admin's", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([validEntry])
    );
    window.localStorage.setItem(
      "nth-dao-recently-added:alice",
      JSON.stringify([{ ...validEntry, agent_id: "bob-secret" }])
    );

    render(<ContactsPanel actorId="alice" />);
    expect(screen.getByText("bob-secret")).toBeTruthy();
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("legacy unscoped key (nth-dao-recently-added) is NOT consulted", () => {
    // Pre-fix code wrote to the unscoped key. If a user upgrades after
    // having data in the legacy slot, we deliberately do NOT honor it -
    // the data may belong to a different actor and reading it would
    // re-introduce the cross-actor leak.
    window.localStorage.setItem(
      "nth-dao-recently-added",
      JSON.stringify([{ ...validEntry, agent_id: "legacy-bob" }])
    );

    render(<ContactsPanel actorId="admin" />);
    expect(screen.queryByText("legacy-bob")).toBeNull();
    expect(screen.queryByText("Recently Added")).toBeNull();
  });

  it("empty actorId falls back to a single shared anon bucket", () => {
    // Anonymous bootstrap (before agent_id is set) goes to a stable
    // sentinel so the rail still works, but doesn't pollute a named
    // user's bucket later.
    window.localStorage.setItem(
      "nth-dao-recently-added:__anon__",
      JSON.stringify([validEntry])
    );
    render(<ContactsPanel actorId="" />);
    expect(screen.getByText("alice")).toBeTruthy();
  });
});


// ===== R-3b: swap on actorId change =====


describe("ContactsPanel - reload on actorId change (R-3b)", () => {
  it("a fresh render with a different actorId shows that bucket", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([{ ...validEntry, agent_id: "alice-only" }])
    );
    window.localStorage.setItem(
      "nth-dao-recently-added:bob",
      JSON.stringify([{ ...validEntry, agent_id: "carol-only" }])
    );

    const { unmount } = render(<ContactsPanel actorId="admin" />);
    expect(screen.getByText("alice-only")).toBeTruthy();
    unmount();
    cleanup();

    render(<ContactsPanel actorId="bob" />);
    expect(screen.getByText("carol-only")).toBeTruthy();
    expect(screen.queryByText("alice-only")).toBeNull();
  });
});


// ===== R-10: structural guard =====


describe("ContactsPanel - tampered localStorage guard (R-10)", () => {
  it("filters out entries that are not RecentlyAdded shape", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        null,
        "just a string",
        { foo: "bar" },
        { agent_id: 12345 },                    // wrong type
        { ...validEntry, agent_id: "good-row" }, // valid
      ])
    );

    render(<ContactsPanel actorId="admin" />);
    // Only the well-formed entry survives.
    expect(screen.getByText("good-row")).toBeTruthy();
    expect(screen.queryByText("just a string")).toBeNull();
  });

  it("hides the rail when localStorage holds only garbage", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([null, "x", { foo: 1 }])
    );
    render(<ContactsPanel actorId="admin" />);
    expect(screen.queryByText("Recently Added")).toBeNull();
  });

  it("survives malformed JSON without throwing", () => {
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      "not valid json at all"
    );
    // Should not crash; rail hidden.
    expect(() => render(<ContactsPanel actorId="admin" />)).not.toThrow();
    expect(screen.queryByText("Recently Added")).toBeNull();
  });
});


// ===== Save path scoping =====


describe("ContactsPanel - save uses the scoped key (R-3)", () => {
  it("clicking + Add writes to the actor-scoped key", async () => {
    vi.spyOn(contactsModule, "searchAgents").mockResolvedValue([
      {
        agent_id: "newone",
        score: 1,
        status: "offline",
        hostname: "",
        backend_id: "",
        capabilities: [],
        groups: [],
        last_seen: "",
        matched: [],
        code: "ffff-eeee",
        source: "home",
      } as contactsModule.AgentMatch,
    ]);
    vi.spyOn(contactsModule, "addAgent").mockResolvedValue({
      ok: true,
      agent_id: "newone",
      did: "",
      label: "",
    });

    render(<ContactsPanel actorId="charlie" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "newone" },
    });
    fireEvent.click(screen.getByText("Search"));
    await screen.findByText("newone");
    fireEvent.click(screen.getByText("+ Add"));

    await waitFor(() => {
      const raw = window.localStorage.getItem(
        "nth-dao-recently-added:charlie"
      );
      expect(raw).not.toBeNull();
      const parsed = JSON.parse(raw!);
      expect(parsed).toHaveLength(1);
      expect(parsed[0].agent_id).toBe("newone");
    });
    // And the legacy unscoped key was NOT written.
    expect(window.localStorage.getItem("nth-dao-recently-added")).toBeNull();
    // And another actor's bucket is untouched.
    expect(
      window.localStorage.getItem("nth-dao-recently-added:admin")
    ).toBeNull();
  });
});
