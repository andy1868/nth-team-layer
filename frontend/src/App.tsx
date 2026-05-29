import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  createChannel,
  createTask,
  getState,
  getSummary,
  join,
  postAnnouncement,
  postMessage,
  updateTaskStatus
} from "./api";
import type { DaoState, Summary, TaskStatus } from "./types";

const defaultAgent = window.localStorage.getItem("nth-dao-agent-id") || "admin";
const taskStatuses: TaskStatus[] = ["open", "accepted", "running", "blocked", "completed", "cancelled"];

function App() {
  const [agentId, setAgentId] = useState(defaultAgent);
  const [selectedChannel, setSelectedChannel] = useState("general");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [state, setState] = useState<DaoState | null>(null);
  const [messageBody, setMessageBody] = useState("");
  const [announcementTitle, setAnnouncementTitle] = useState("");
  const [announcementBody, setAnnouncementBody] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskDescription, setTaskDescription] = useState("");
  const [taskAssignee, setTaskAssignee] = useState("");
  const [channelName, setChannelName] = useState("");
  const [channelTopic, setChannelTopic] = useState("");
  const [notice, setNotice] = useState("Loading console state...");
  const [busy, setBusy] = useState(false);

  const activeChannel = useMemo(
    () => state?.channels.find((channel) => channel.channel_id === selectedChannel),
    [selectedChannel, state?.channels]
  );

  async function refresh(nextAgent = agentId, nextChannel = selectedChannel) {
    const cleanAgent = nextAgent.trim() || "admin";
    const [summaryData, stateData] = await Promise.all([
      getSummary(),
      getState(cleanAgent, nextChannel)
    ]);
    setSummary(summaryData);
    setState(stateData);
    setNotice("Ready");
  }

  useEffect(() => {
    refresh().catch((error: Error) => setNotice(error.message));
    const id = window.setInterval(() => {
      refresh().catch((error: Error) => setNotice(error.message));
    }, 5000);
    return () => window.clearInterval(id);
  }, []);

  async function run(action: () => Promise<void>, done = "Updated") {
    setBusy(true);
    setNotice("Working...");
    try {
      await action();
      await refresh();
      setNotice(done);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  function updateAgent(value: string) {
    setAgentId(value);
    window.localStorage.setItem("nth-dao-agent-id", value);
  }

  async function onJoin(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await join(agentId.trim() || "admin");
    }, "Agent joined");
  }

  async function onCreateChannel(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await createChannel({
        actorId: agentId,
        name: channelName,
        topic: channelTopic,
        isPrivate: false
      });
      setChannelName("");
      setChannelTopic("");
    }, "Channel created");
  }

  async function onPostMessage(event: FormEvent) {
    event.preventDefault();
    if (!messageBody.trim()) return;
    await run(async () => {
      await postMessage({ agentId, channelId: selectedChannel, body: messageBody });
      setMessageBody("");
    }, "Message posted");
  }

  async function onPostAnnouncement(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await postAnnouncement({
        authorId: agentId,
        channelId: selectedChannel,
        title: announcementTitle,
        body: announcementBody
      });
      setAnnouncementTitle("");
      setAnnouncementBody("");
    }, "Announcement posted");
  }

  async function onCreateTask(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await createTask({
        createdBy: agentId,
        channelId: selectedChannel,
        title: taskTitle,
        description: taskDescription,
        assigneeId: taskAssignee
      });
      setTaskTitle("");
      setTaskDescription("");
      setTaskAssignee("");
    }, "Task created");
  }

  async function onUpdateTask(taskId: string, status: TaskStatus) {
    await run(async () => {
      await updateTaskStatus({ taskId, actorId: agentId, status });
    }, "Task status updated");
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Local-first DAO group layer</p>
          <h1>NTH DAO Console</h1>
        </div>
        <div className="status-strip" aria-live="polite">
          <Metric label="Members" value={summary?.members ?? "-"} />
          <Metric label="Channels" value={summary?.channels ?? "-"} />
          <Metric label="Tasks" value={summary?.tasks ?? "-"} />
          <Metric label="Role" value={state?.actor.role ?? "-"} />
        </div>
      </header>

      <section className="workspace">
        <aside className="left-rail">
          <form className="panel" onSubmit={onJoin}>
            <label htmlFor="agent-id">Local agent id</label>
            <div className="inline">
              <input
                id="agent-id"
                value={agentId}
                onChange={(event) => updateAgent(event.target.value)}
                spellCheck={false}
              />
              <button type="submit" disabled={busy}>Join</button>
            </div>
            <p className="hint">Current membership policy: {summary?.team.join_policy ?? "loading"}</p>
          </form>

          <section className="panel">
            <div className="panel-heading">
              <h2>Channels</h2>
              <span>{state?.channels.length ?? 0}</span>
            </div>
            <div className="stack">
              {state?.channels.map((channel) => (
                <button
                  className={channel.channel_id === selectedChannel ? "channel active" : "channel"}
                  key={channel.channel_id}
                  onClick={() => {
                    setSelectedChannel(channel.channel_id);
                    refresh(agentId, channel.channel_id).catch((error: Error) => setNotice(error.message));
                  }}
                  type="button"
                >
                  <span>#{channel.name}</span>
                  <small>{channel.topic || "No topic"}</small>
                </button>
              ))}
            </div>
          </section>

          <form className="panel" onSubmit={onCreateChannel}>
            <div className="panel-heading">
              <h2>New channel</h2>
            </div>
            <input
              placeholder="Name"
              value={channelName}
              onChange={(event) => setChannelName(event.target.value)}
            />
            <input
              placeholder="Topic"
              value={channelTopic}
              onChange={(event) => setChannelTopic(event.target.value)}
            />
            <button type="submit" disabled={busy || !channelName.trim()}>Create</button>
          </form>
        </aside>

        <section className="conversation">
          <div className="conversation-head">
            <div>
              <p className="eyebrow">Channel</p>
              <h2>#{activeChannel?.name ?? selectedChannel}</h2>
            </div>
            <span className="notice">{notice}</span>
          </div>

          <div className="announcement-list">
            {state?.announcements.map((announcement) => (
              <article className="announcement" key={announcement.announcement_id}>
                <strong>{announcement.title}</strong>
                <p>{announcement.body}</p>
                <small>{announcement.author_id} - {shortTime(announcement.created_at)}</small>
              </article>
            ))}
          </div>

          <div className="messages">
            {state?.messages.length ? state.messages.map((message) => (
              <article className="message" key={message.message_id}>
                <div>
                  <strong>{message.sender_id}</strong>
                  <time>{shortTime(message.created_at)}</time>
                </div>
                <p>{message.body}</p>
              </article>
            )) : <p className="empty">No messages in this channel yet.</p>}
          </div>

          <form className="composer" onSubmit={onPostMessage}>
            <textarea
              placeholder="Write a message to this DAO channel"
              value={messageBody}
              onChange={(event) => setMessageBody(event.target.value)}
            />
            <button type="submit" disabled={busy || !messageBody.trim()}>Send</button>
          </form>
        </section>

        <aside className="right-rail">
          <section className="panel">
            <div className="panel-heading">
              <h2>Members</h2>
              <span>{state?.members.length ?? 0}</span>
            </div>
            <div className="member-list">
              {state?.members.map((member) => (
                <div className="member" key={member.agent_id}>
                  <span>{member.agent_id}</span>
                  <small>{member.role}{member.online ? " online" : ""}</small>
                </div>
              ))}
            </div>
          </section>

          <form className="panel" onSubmit={onPostAnnouncement}>
            <div className="panel-heading">
              <h2>Announcement</h2>
            </div>
            <input
              placeholder="Title"
              value={announcementTitle}
              onChange={(event) => setAnnouncementTitle(event.target.value)}
            />
            <textarea
              placeholder="Body"
              value={announcementBody}
              onChange={(event) => setAnnouncementBody(event.target.value)}
            />
            <button type="submit" disabled={busy || !announcementTitle.trim()}>Post</button>
          </form>

          <form className="panel" onSubmit={onCreateTask}>
            <div className="panel-heading">
              <h2>Task</h2>
            </div>
            <input
              placeholder="Title"
              value={taskTitle}
              onChange={(event) => setTaskTitle(event.target.value)}
            />
            <input
              placeholder="Assignee id"
              value={taskAssignee}
              onChange={(event) => setTaskAssignee(event.target.value)}
            />
            <textarea
              placeholder="Description"
              value={taskDescription}
              onChange={(event) => setTaskDescription(event.target.value)}
            />
            <button type="submit" disabled={busy || !taskTitle.trim()}>Create</button>
          </form>

          <section className="panel">
            <div className="panel-heading">
              <h2>Tasks</h2>
              <span>{state?.tasks.length ?? 0}</span>
            </div>
            <div className="stack">
              {state?.tasks.map((task) => (
                <article className="task" key={task.task_id}>
                  <strong>{task.title}</strong>
                  <small>{task.assignee_id || "unassigned"} - {task.status}</small>
                  <select
                    value={task.status}
                    onChange={(event) => onUpdateTask(task.task_id, event.target.value as TaskStatus)}
                  >
                    {taskStatuses.map((status) => (
                      <option key={status} value={status}>{status}</option>
                    ))}
                  </select>
                </article>
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-heading">
              <h2>Audit</h2>
              <span>{state?.audit.length ?? 0}</span>
            </div>
            <div className="audit-list">
              {state?.audit.slice().reverse().map((event) => (
                <article className="audit" key={event.event_id}>
                  <strong>{event.event_type}</strong>
                  <span>{event.summary}</span>
                  <small>{event.actor_id} - {shortTime(event.created_at)}</small>
                </article>
              ))}
            </div>
          </section>
        </aside>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function shortTime(value: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export default App;
