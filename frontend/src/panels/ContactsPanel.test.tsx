// Week-1 (2026-06-07): regression tests for the enriched ContactsPanel.
//
// Pins:
//   T1 - search result row displays code, source badge, role
//   T1 - endorsement_count > 0 surfaces as a "🟢 N endorsements" line
//   T1 - endorsement_count == 0 / missing hides the line entirely
//   T3 - "+ Add" pushes a row into the Recently Added rail
//   T3 - Recently Added is hidden when localStorage is empty
//   T3 - Recently Added loads from localStorage on mount
//   T3 - dedupes by agent_id (re-add moves to top, doesn't duplicate)

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

function mockSearch(rows: Partial<contactsModule.AgentMatch>[]) {
  return vi
    .spyOn(contactsModule, "searchAgents")
    .mockResolvedValue(
      rows.map((r) => ({
        agent_id: r.agent_id ?? "x",
        score: r.score ?? 1,
        status: r.status ?? "offline",
        hostname: r.hostname ?? "",
        backend_id: r.backend_id ?? "",
        capabilities: r.capabilities ?? [],
        groups: r.groups ?? [],
        last_seen: r.last_seen ?? "",
        matched: r.matched ?? [],
        ...r,
      })) as contactsModule.AgentMatch[]
    );
}


// ===== T1: rich identity rendering =====


describe("ContactsPanel - search row identity (Task 1)", () => {
  it("renders code, source badge, and role on every row", async () => {
    mockSearch([
      {
        agent_id: "alice",
        code: "8c69-76e5",
        source: "home",
        role: "owner",
      },
    ]);
    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "alice" },
    });
    fireEvent.click(screen.getByText("Search"));

    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.getByText("8c69-76e5")).toBeTruthy();
    expect(screen.getByText("team")).toBeTruthy(); // home -> team badge
    expect(screen.getByText("owner")).toBeTruthy();
  });

  it("surfaces endorsement_count when > 0", async () => {
    mockSearch([
      {
        agent_id: "bob",
        code: "abcd-1234",
        source: "group",
        role: "member",
        endorsement_count: 12,
      },
    ]);
    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "bob" },
    });
    fireEvent.click(screen.getByText("Search"));

    expect(await screen.findByText(/12 endorsements/)).toBeTruthy();
  });

  it("hides endorsement_count line when 0 or missing", async () => {
    mockSearch([
      {
        agent_id: "unknown-agent",
        code: "ffff-0000",
        source: "group",
        endorsement_count: 0,
      },
    ]);
    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "unknown" },
    });
    fireEvent.click(screen.getByText("Search"));

    expect(await screen.findByText("unknown-agent")).toBeTruthy();
    expect(screen.queryByText(/endorsement/)).toBeNull();
  });
});


// ===== T3: Recently Added persistence =====


describe("ContactsPanel - Recently Added (Task 3)", () => {
  it("hides the Recently Added rail when localStorage is empty", () => {
    render(<ContactsPanel actorId="admin" />);
    expect(screen.queryByText("Recently Added")).toBeNull();
  });

  it("pushes a row into Recently Added after a successful + Add", async () => {
    mockSearch([{ agent_id: "carol", code: "1111-2222", source: "home" }]);
    vi.spyOn(contactsModule, "addAgent").mockResolvedValue({
      ok: true,
      agent_id: "carol",
      did: "",
      label: "",
    });

    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "carol" },
    });
    fireEvent.click(screen.getByText("Search"));
    await screen.findByText("carol");

    fireEvent.click(screen.getByText("+ Add"));

    const recentHeading = await screen.findByText("Recently Added");
    expect(recentHeading).toBeTruthy();
    const recentSection = recentHeading.parentElement!;
    expect(recentSection.textContent).toContain("carol");
  });

  it("loads the Recently Added rail from localStorage on mount", async () => {
    // Architect R-3 (2026-06-07): the storage key is now scoped per
    // actorId, so this test writes to ``nth-dao-recently-added:admin``
    // rather than the legacy single bucket.
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        {
          agent_id: "dave",
          did: "",
          label: "the integrator",
          added_at: new Date().toISOString(),
        },
      ])
    );
    render(<ContactsPanel actorId="admin" />);
    await waitFor(() => {
      expect(screen.getByText("Recently Added")).toBeTruthy();
    });
    expect(screen.getByText("dave")).toBeTruthy();
    expect(screen.getByText(/the integrator/)).toBeTruthy();
  });

  it("dedupes by agent_id so re-adding moves to top, not duplicate", async () => {
    mockSearch([{ agent_id: "eve", code: "eeee-eeee", source: "home" }]);
    vi.spyOn(contactsModule, "addAgent").mockResolvedValue({
      ok: true,
      agent_id: "eve",
      did: "",
      label: "",
    });

    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/i), {
      target: { value: "eve" },
    });
    fireEvent.click(screen.getByText("Search"));
    await screen.findByText("eve");

    fireEvent.click(screen.getByText("+ Add"));
    await screen.findByText("Recently Added");
    fireEvent.click(screen.getByText("+ Add"));

    await waitFor(() => {
      const recentSection =
        screen.getByText("Recently Added").parentElement!;
      const matches =
        (recentSection.textContent ?? "").match(/eve/g) ?? [];
      // Recently Added has exactly one "eve"; the result row above
      // also contains "eve", but we scope to recentSection only.
      expect(matches.length).toBe(1);
    });
  });
});
