import { useCallback, useEffect, useRef, useState } from "react";
import { createChannel, createTask, getDaoState, getDaos, getSummary, join, lookupAgentByCode, postAnnouncement, postMessage, updateTaskStatus } from "./api";
import type { BrowserWallet } from "./crypto";
import { loadOrCreateWallet } from "./crypto";
import type { DaoState, DaoSummary, Summary, TaskStatus } from "./types";

import { Topbar } from "./components/Topbar";
import { IconNav } from "./components/IconNav";
import type { NavItem } from "./components/IconNav";
import { ChatSidebar } from "./components/SidebarChat";
import { PeopleSidebar } from "./components/SidebarPeople";
import { ChatArea, EmptyPanel, PanelAnnounce, PanelAudit, PanelTasks } from "./components/Panels";
import { scopedChannelId } from "./components/utils";

/* ── constants ── */
const defaultAgent = window.localStorage.getItem("nth-dao-agent-id") || "admin";
const defaultDao = window.localStorage.getItem("nth-dao-active-slug") || "home";
const POLL_MS = 5000;

type SidebarMode = "chat" | "people";
type DetailTab = "tasks" | "announce" | "audit";

/* ═══════════════════════════ App ═══════════════════════════ */

export default function App() {
  /* ── core state ── */
  const [agentId, setAgentId] = useState(defaultAgent);
  const [activeDao, setActiveDao] = useState(defaultDao);
  const [daos, setDaos] = useState<DaoSummary[]>([]);
  const [selectedChannel, setSelectedChannel] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [state, setState] = useState<DaoState | null>(null);

  /* ── ui state ── */
  const [notice, setNotice] = useState("Loading…");
  const [busy, setBusy] = useState(false);
  const [sidebarMode, setSidebarMode] = useState<SidebarMode>("chat");
  const [detailTab, setDetailTab] = useState<DetailTab | null>(null);

  /* ── wallet ── */
  const [wallet, setWallet] = useState<BrowserWallet | null>(null);
  const [walletError, setWalletError] = useState<string | null>(null);

  /* ── refs ── */
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /* ── wallet bootstrap ── */
  useEffect(() => {
    let cancelled = false;
    loadOrCreateWallet()
      .then(w => { if (!cancelled) setWallet(w); })
      .catch((e: Error) => { if (!cancelled) setWalletError(e.message); });
    return () => { cancelled = true; };
  }, []);

  /* ── data refresh ── */
  const refresh = useCallback(async (aid?: string, ch?: string, dao?: string) => {
    const a = (aid ?? agentId).trim() || "admin";
    const d = dao ?? activeDao;
    const c = ch ?? selectedChannel;
    try {
      const [s, st] = await Promise.all([getSummary(a), getDaoState(d, a, c)]);
      setSummary(s); setState(st);
      if (!c && st.active_channel_id) setSelectedChannel(st.active_channel_id);
      setNotice("Ready");
    } catch (e) {
      setNotice((e as Error).message);
    }
  }, [agentId, activeDao, selectedChannel]);

  const refreshDaos = useCallback(async (pk: string) => {
    try { setDaos((await getDaos(agentId, pk)).daos); }
    catch (e) { /* silent — sidebar failure shouldn't break main view */ }
  }, [agentId]);

  /* ── polling ── */
  useEffect(() => {
    refresh();
    refreshDaos(wallet?.pubkeyHex ?? "");

    pollingRef.current = setInterval(() => {
      refresh();
      refreshDaos(wallet?.pubkeyHex ?? "");
    }, POLL_MS);

    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [activeDao, wallet?.pubkeyHex, refresh, refreshDaos]);

  /* ── actions ── */
  const switchDao = useCallback((slug: string) => {
    if (slug === activeDao) return;
    setActiveDao(slug); setSelectedChannel("");
    localStorage.setItem("nth-dao-active-slug", slug);
  }, [activeDao]);

  const run = useCallback(async (fn: () => Promise<unknown>, done = "Updated") => {
    setBusy(true); setNotice("Working…");
    try { await fn(); await refresh(); setNotice(done); }
    catch (e) { setNotice(e instanceof Error ? e.message : "Failed"); }
    finally { setBusy(false); }
  }, [refresh]);

  /* ── handlers ── */
  const handleJoin = useCallback(() => {
    run(() => join(agentId.trim() || "admin"), "Joined");
  }, [agentId, run]);

  const handleCreateChannel = useCallback(async (name: string, topic: string) => {
    await run(async () => {
      await createChannel({
        actorId: agentId, name, topic, isPrivate: false,
        channelId: scopedChannelId(activeDao, name.trim()),
      });
    }, "Created");
  }, [agentId, activeDao, run]);

  const handleSend = useCallback(async (body: string) => {
    await run(async () => {
      await postMessage({ agentId, channelId: selectedChannel, body });
    }, "Sent");
  }, [agentId, selectedChannel, run]);

  const handlePostAnnounce = useCallback(async (title: string, body: string) => {
    await run(async () => {
      await postAnnouncement({ authorId: agentId, channelId: selectedChannel, title, body });
    }, "Posted");
  }, [agentId, selectedChannel, run]);

  const handleCreateTask = useCallback(async (title: string, desc: string, assignee: string) => {
    await run(async () => {
      await createTask({ createdBy: agentId, channelId: selectedChannel, title, description: desc, assigneeId: assignee });
    }, "Created");
  }, [agentId, selectedChannel, run]);

  const handleUpdateTask = useCallback((taskId: string, status: TaskStatus) => {
    run(() => updateTaskStatus({ taskId, actorId: agentId, status }), "Updated");
  }, [agentId, run]);

  const handleLookupAgent = useCallback(async (code: string) => {
    const hit = await lookupAgentByCode(code.trim());
    const where = hit.source === "group" ? `in @${hit.group_slug}` : "in home";
    setNotice(`Found ${hit.agent_id} (${hit.code}) ${where}`);
  }, []);

  /* ── nav ── */
  const handleNav = useCallback((item: NavItem) => {
    if (item.kind === "sidebar") {
      setSidebarMode(item.id as SidebarMode);
      setDetailTab(null);
    } else {
      setDetailTab(prev => prev === item.id ? null : item.id as DetailTab);
    }
  }, []);

  const handleSelectChannel = useCallback((ch: string) => {
    setSelectedChannel(ch);
    refresh(agentId, ch).catch(e => setNotice((e as Error).message));
  }, [agentId, refresh]);

  const handleAgentIdChange = useCallback((v: string) => {
    setAgentId(v);
    localStorage.setItem("nth-dao-agent-id", v);
  }, []);

  /* ── derived ── */
  const taskCount = state?.tasks.length ?? 0;

  /* ═══════════════════════════ RENDER ═══════════════════════════ */
  return (
    <main className="shell">
      <Topbar summary={summary} actorRole={state?.actor.role ?? "—"} />

      <section className="workspace">
        <IconNav
          activeSidebar={sidebarMode}
          activeDetail={detailTab}
          taskCount={taskCount}
          onNav={handleNav}
        />

        <aside className="left-rail">
          {sidebarMode === "chat" ? (
            <ChatSidebar
              agentId={agentId}
              onAgentIdChange={handleAgentIdChange}
              summary={summary}
              busy={busy}
              onJoin={handleJoin}
              daos={daos}
              activeDao={activeDao}
              onSwitchDao={switchDao}
              channels={state?.channels ?? []}
              selectedChannel={selectedChannel}
              onSelectChannel={handleSelectChannel}
              onCreateChannel={handleCreateChannel}
            />
          ) : (
            <PeopleSidebar
              members={state?.members ?? []}
              agentId={agentId}
              busy={busy}
              wallet={wallet}
              walletError={walletError}
              onLookupAgent={handleLookupAgent}
            />
          )}
        </aside>

        <ChatArea
          state={state}
          notice={notice}
          busy={busy}
          selectedChannel={selectedChannel}
          onSend={handleSend}
        />

        <aside className="right-rail">
          {detailTab === null ? <EmptyPanel /> :
           detailTab === "tasks" ? (
            <PanelTasks
              tasks={state?.tasks ?? []}
              busy={busy}
              onCreateTask={handleCreateTask}
              onUpdateTask={handleUpdateTask}
            />
          ) : detailTab === "announce" ? (
            <PanelAnnounce
              busy={busy}
              onPost={handlePostAnnounce}
              announcements={state?.announcements ?? []}
            />
          ) : detailTab === "audit" ? (
            <PanelAudit audit={state?.audit ?? []} />
          ) : null}
        </aside>
      </section>
    </main>
  );
}
