import type { TaskStatus } from "../types";

export const taskStatuses: TaskStatus[] = ["open", "accepted", "running", "blocked", "completed", "cancelled"];

export function shortTime(v: string): string {
  if (!v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export function scopedChannelId(slug: string, bare: string): string {
  if (!bare) return bare;
  if (!slug || slug === "home") return bare;
  const prefix = `dao-${slug}-`;
  return bare.startsWith(prefix) ? bare : prefix + bare;
}
