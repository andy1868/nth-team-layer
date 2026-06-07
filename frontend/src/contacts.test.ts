// Architect audit (2026-06-07): regression tests for the
// frontend-side ``/api/agents/search`` contract.
//
// Bug history:
//   * PR #10 wired up the backend friend-search endpoint but its
//     return-shape was hidden behind a collapsed <details> on the
//     dashboard, so users never saw the UI.
//   * Architect audit C-1 then tightened the backend to require an
//     ``actor_id`` query parameter, which the original frontend
//     ``searchAgents`` did NOT pass - so even after the user opened
//     the panel and clicked Search, the request failed with HTTP 400.
//
// These tests pin the post-fix invariants so neither regression can
// silently slip back in.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { searchAgents } from "./contacts";

// fetch is provided by jsdom in the vitest env. We replace it with a
// spy that captures the URL the SUT calls, so we can assert on the
// query-string contents without spinning up a real backend.

interface CapturedCall {
  url: string;
  init?: RequestInit;
}

let captured: CapturedCall[];
let originalFetch: typeof fetch;

beforeEach(() => {
  captured = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = vi.fn(
    async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      captured.push({ url: String(url), init });
      return new Response(JSON.stringify({ results: [] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      });
    }
  ) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("searchAgents", () => {
  it("returns an empty array when the query is blank without making any HTTP call", async () => {
    const out = await searchAgents("   ");
    expect(out).toEqual([]);
    expect(captured).toHaveLength(0);
  });

  it("passes the caller's actorId as the actor_id query parameter", async () => {
    // C-1: this is the regression net. The original call signature was
    // searchAgents(query, limit) - no actor_id - and dropped silently
    // into a 400 after the backend gate landed.
    await searchAgents("alice", 5, "bob");
    expect(captured).toHaveLength(1);
    const url = new URL(captured[0].url, "http://localhost");
    expect(url.pathname).toBe("/api/agents/search");
    expect(url.searchParams.get("q")).toBe("alice");
    expect(url.searchParams.get("limit")).toBe("5");
    expect(url.searchParams.get("actor_id")).toBe("bob");
  });

  it("falls back to actor_id='admin' when no caller is supplied", async () => {
    // The default-arg fallback keeps single-arg legacy callers from
    // 400'ing in dev / tests where they didn't know to thread actorId.
    await searchAgents("alice");
    const url = new URL(captured[0].url, "http://localhost");
    expect(url.searchParams.get("actor_id")).toBe("admin");
  });

  it("URL-encodes the actor_id parameter to survive special characters", async () => {
    await searchAgents("query", 10, "agent@example.com");
    const url = new URL(captured[0].url, "http://localhost");
    // URLSearchParams escapes '@' as '%40' on round-trip
    expect(url.searchParams.get("actor_id")).toBe("agent@example.com");
    expect(captured[0].url).toContain("actor_id=agent%40example.com");
  });

  it("surfaces the backend 'detail' on auth-gate rejection", async () => {
    // Simulate the gate firing because the caller is not a member.
    globalThis.fetch = vi.fn(
      async (): Promise<Response> =>
        new Response(
          JSON.stringify({ detail: "agent 'stranger' is not a member" }),
          { status: 403, headers: { "content-type": "application/json" } }
        )
    ) as typeof fetch;
    await expect(searchAgents("anyone", 10, "stranger")).rejects.toThrow(
      /not a member/
    );
  });
});
