// Architect R-16 (2026-06-07): Recently Added dedup uses a canonical
// handle so the same identity added via different routes (agent_id /
// DID) collapses to one row.

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


function mockAdd() {
  vi.spyOn(contactsModule, "addAgent").mockResolvedValue({
    ok: true,
    agent_id: "",
    did: "",
    label: "",
  });
}


describe("ContactsPanel Recently Added dedup (R-16)", () => {
  it("collapses same agent_id added twice into one row", async () => {
    mockAdd();
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        {
          agent_id: "alice",
          did: "",
          label: "first add",
          added_at: "2026-06-07T10:00:00Z",
        },
      ])
    );
    render(<ContactsPanel actorId="admin" />);
    // Use the direct-add path so we don't need to mock the search
    fireEvent.change(screen.getByPlaceholderText("agent_id"), {
      target: { value: "alice" },
    });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      const section = screen.getByText("Recently Added").parentElement!;
      const matches = (section.textContent ?? "").match(/alice/g) ?? [];
      expect(matches.length).toBe(1);
    });
  });

  it("collapses agent_id and matching DID add into one row when handle matches", async () => {
    mockAdd();
    // First entry pre-seeded with agent_id="alice"
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        {
          agent_id: "alice",
          did: "",
          label: "by id",
          added_at: "2026-06-07T10:00:00Z",
        },
      ])
    );
    render(<ContactsPanel actorId="admin" />);
    // Now add the SAME alice but supplied via the DID slot. The
    // canonical handle is the same string "alice", so dedup applies.
    fireEvent.change(screen.getByPlaceholderText("did:key:z6Mk..."), {
      target: { value: "alice" },
    });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      const section = screen.getByText("Recently Added").parentElement!;
      const matches = (section.textContent ?? "").match(/alice/g) ?? [];
      expect(matches.length).toBe(1);
    });
  });

  it("treats different handles as distinct entries", async () => {
    mockAdd();
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        {
          agent_id: "alice",
          did: "",
          label: "",
          added_at: "2026-06-07T10:00:00Z",
        },
      ])
    );
    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText("agent_id"), {
      target: { value: "bob" },
    });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      const section = screen.getByText("Recently Added").parentElement!;
      expect(section.textContent).toContain("alice");
      expect(section.textContent).toContain("bob");
    });
  });

  it("is case-insensitive on canonical handle", async () => {
    mockAdd();
    window.localStorage.setItem(
      "nth-dao-recently-added:admin",
      JSON.stringify([
        {
          agent_id: "Alice",
          did: "",
          label: "uppercase",
          added_at: "2026-06-07T10:00:00Z",
        },
      ])
    );
    render(<ContactsPanel actorId="admin" />);
    fireEvent.change(screen.getByPlaceholderText("agent_id"), {
      target: { value: "ALICE" },
    });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      const section = screen.getByText("Recently Added").parentElement!;
      const matches = (section.textContent ?? "").match(/alice/gi) ?? [];
      expect(matches.length).toBe(1);
    });
  });
});
