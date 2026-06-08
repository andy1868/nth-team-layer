import type { Summary, DaoState } from "../types";

export interface MetricProps {
  label: string;
  value: string | number;
}

export function Metric({ label, value }: MetricProps) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export interface TopbarProps {
  summary: Pick<Summary, "members" | "channels" | "tasks"> | null;
  actorRole: string;
}

export function Topbar({ summary, actorRole }: TopbarProps) {
  return (
    <header className="topbar">
      <div className="topbar-brand">
        <div className="topbar-logo">N</div>
        <h1>Nth DAO</h1>
      </div>
      <div className="status-strip" aria-live="polite">
        <Metric label="Members" value={summary?.members ?? "—"} />
        <Metric label="Channels" value={summary?.channels ?? "—"} />
        <Metric label="Tasks" value={summary?.tasks ?? "—"} />
        <Metric label="Role" value={actorRole || "—"} />
      </div>
    </header>
  );
}
