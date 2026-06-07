declare global {
  interface Window {
    __NTH_CONSOLE_TOKEN__?: string;
  }
}

export function getConsoleToken(): string {
  if (typeof window === "undefined") return "";
  const token = window.__NTH_CONSOLE_TOKEN__;
  return typeof token === "string" ? token : "";
}

export function jsonHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers);
  if (!headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const token = getConsoleToken();
  if (token && !headers.has("authorization")) {
    headers.set("authorization", `Bearer ${token}`);
  }
  return headers;
}

export {};
