import { afterEach, describe, expect, it } from "vitest";
import { jsonHeaders } from "./consoleAuth";

describe("console auth headers", () => {
  afterEach(() => {
    delete window.__NTH_CONSOLE_TOKEN__;
  });

  it("adds the console Bearer token when injected by the server", () => {
    window.__NTH_CONSOLE_TOKEN__ = "secret-token";

    const headers = jsonHeaders();

    expect(headers.get("authorization")).toBe("Bearer secret-token");
    expect(headers.get("content-type")).toBe("application/json");
  });

  it("preserves an explicit authorization header", () => {
    window.__NTH_CONSOLE_TOKEN__ = "server-token";

    const headers = jsonHeaders({
      headers: { authorization: "Bearer caller-token" }
    });

    expect(headers.get("authorization")).toBe("Bearer caller-token");
  });
});
