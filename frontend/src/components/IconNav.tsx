import { useState } from "react";

export interface NavItem {
  id: string;
  kind: "sidebar" | "detail";
  icon: string;
  label: string;
}

/* ── Primary nav: always visible ── */
const PRIMARY: NavItem[] = [
  { id: "chat",   kind: "sidebar", icon: "💬", label: "Chat" },
  { id: "people", kind: "sidebar", icon: "👥", label: "People" },
];

/* ── Secondary: tucked under "More" ── */
const SECONDARY: NavItem[] = [
  { id: "tasks",    kind: "detail", icon: "✅", label: "Tasks" },
  { id: "announce", kind: "detail", icon: "📢", label: "Announce" },
  { id: "audit",    kind: "detail", icon: "📜", label: "Audit" },
];

export interface IconNavProps {
  activeSidebar: string;
  activeDetail: string | null;
  taskCount: number;
  onNav: (item: NavItem) => void;
}

export function IconNav({ activeSidebar, activeDetail, taskCount, onNav }: IconNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);

  function isActive(item: NavItem): boolean {
    return item.kind === "sidebar" ? activeSidebar === item.id : activeDetail === item.id;
  }

  const hasSecondaryActive = SECONDARY.some(isActive);

  return (
    <nav className="icon-nav">
      {/* Primary icons */}
      {PRIMARY.map(item => (
        <button
          key={item.id}
          className={`icon-nav-btn ${isActive(item) ? "active" : ""}`}
          onClick={() => onNav(item)}
          title={item.label}
        >
          <span>{item.icon}</span>
          <span className="icon-label">{item.label}</span>
        </button>
      ))}

      {/* Divider */}
      <div className="icon-nav-spacer" />

      {/* More button */}
      <div className="icon-nav-more" style={{ position: "relative" }}>
        <button
          className={`icon-nav-btn ${hasSecondaryActive ? "active" : ""}`}
          onClick={() => setMoreOpen(!moreOpen)}
          title="More"
        >
          <span>···</span>
          <span className="icon-label">More</span>
          {taskCount > 0 && <span className="badge">{taskCount}</span>}
        </button>

        {moreOpen && (
          <>
            <div className="icon-nav-overlay" onClick={() => setMoreOpen(false)} />
            <div className="icon-nav-popover">
              {SECONDARY.map(item => (
                <button
                  key={item.id}
                  className={`icon-nav-popover-btn ${isActive(item) ? "active" : ""}`}
                  onClick={() => { onNav(item); setMoreOpen(false); }}
                >
                  <span>{item.icon}</span>
                  <span>{item.label}</span>
                  {item.id === "tasks" && taskCount > 0 && (
                    <span className="badge-inline">{taskCount}</span>
                  )}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </nav>
  );
}
