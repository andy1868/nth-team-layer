"""
AgentDaemon — auto-poll channels and respond via LLM backend.

The daemon bridges the gap between "registered agent" and "live agent".
Without it, agents exist on disk but never respond to messages.

Usage:

    import nth_dao as nth

    team = nth.attach(agent_id="analyst", backend="hermes", workspace="./ws")

    # Start daemon — polls for new messages every 15s, responds via backend
    daemon = nth.AgentDaemon(team, poll_interval=15)
    daemon.start()   # non-blocking (background thread)

    # ... later ...
    daemon.stop()    # graceful shutdown

Design:
- Polls all channel message files for new entries since last_seen timestamp
- For each new message from another agent, calls backend.send_turn()
- Posts the LLM response back to the channel
- Runs in a background daemon thread that stops cleanly on stop() or detach()
- Respects channel membership: only monitors channels the agent belongs to
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set

if TYPE_CHECKING:
    from .attach import TeamSession

logger = logging.getLogger("nth_dao.agent_daemon")


@dataclass
class DaemonConfig:
    """Tuning knobs for the agent daemon."""
    poll_interval: float = 15.0          # seconds between channel polls
    max_response_length: int = 500        # truncate LLM responses
    respond_to_self: bool = False         # whether to respond to own messages
    channel_ids: Optional[List[str]] = None  # None = all accessible channels
    system_prompt: str = ""               # injected before user context
    idle_message: str = ""                # posted when no new messages (empty = silent)
    cooldown_seconds: float = 5.0         # min time between responses to same channel


class AgentDaemon:
    """Background daemon that watches channels and auto-responds via LLM.

    Thread safety:
    - start() / stop() are idempotent and thread-safe
    - The poll loop runs in a daemon thread that exits when the main thread exits
    - stop() waits up to 2*poll_interval for the loop to finish
    """

    def __init__(
        self,
        team: "TeamSession",
        config: Optional[DaemonConfig] = None,
    ):
        self.team = team
        self.config = config or DaemonConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Track last-seen timestamp per channel file to detect new messages
        self._last_seen: Dict[str, str] = {}  # channel_id -> ISO timestamp
        self._last_response: Dict[str, float] = {}  # channel_id -> time.monotonic

    # -- Public API --

    def start(self) -> None:
        """Start the daemon poll loop (non-blocking)."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("AgentDaemon already running for %s", self.team.agent_id)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"nth-daemon-{self.team.agent_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("AgentDaemon started for %s (poll_interval=%.1fs)",
                     self.team.agent_id, self.config.poll_interval)

    def stop(self, timeout: Optional[float] = None) -> None:
        """Signal the daemon to stop and wait for the loop to finish."""
        if self._thread is None or not self._thread.is_alive():
            return
        self._stop_event.set()
        wait = timeout or (self.config.poll_interval * 2)
        self._thread.join(timeout=wait)
        if self._thread.is_alive():
            logger.warning("AgentDaemon for %s did not stop within %.1fs",
                           self.team.agent_id, wait)
        else:
            logger.info("AgentDaemon stopped for %s", self.team.agent_id)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- Internal --

    def _run_loop(self) -> None:
        """Main poll loop — runs in background thread."""
        # Initialize last_seen to empty so the first poll processes
        # existing messages (important for catching messages posted before
        # the daemon started)
        gm = self.team.group_manager
        channels = gm.list_channels(actor_id=self.team.agent_id)
        for ch in channels:
            self._last_seen[ch.channel_id] = ""  # start from beginning

        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.error("AgentDaemon poll error for %s: %s",
                             self.team.agent_id, e, exc_info=True)
            self._stop_event.wait(timeout=self.config.poll_interval)

    def _poll_once(self) -> None:
        """Check all accessible channels for new messages and respond."""
        gm = self.team.group_manager
        channels = gm.list_channels(actor_id=self.team.agent_id)

        # Optionally filter to specific channels
        if self.config.channel_ids is not None:
            ch_ids = set(self.config.channel_ids)
            channels = [ch for ch in channels if ch.channel_id in ch_ids]

        for channel in channels:
            self._process_channel(channel.channel_id)

    def _process_channel(self, channel_id: str) -> None:
        """Check one channel for new messages since last_seen."""
        gm = self.team.group_manager
        messages = gm.list_messages(
            channel_id=channel_id,
            actor_id=self.team.agent_id,
        )
        if not messages:
            return

        last_seen = self._last_seen.get(channel_id, "")
        new_msgs = []

        for msg in messages:
            # Skip own messages (unless configured to respond to self)
            if not self.config.respond_to_self and msg.sender_id == self.team.agent_id:
                continue
            # Only process messages newer than last_seen
            if msg.created_at > last_seen:
                new_msgs.append(msg)

        if not new_msgs:
            return

        # Update last_seen to the newest message timestamp
        self._last_seen[channel_id] = max(m.created_at for m in new_msgs)

        # Cooldown: don't spam responses
        now_mono = time.monotonic()
        last_resp = self._last_response.get(channel_id, 0)
        if now_mono - last_resp < self.config.cooldown_seconds:
            return

        # Build context from new messages
        context_lines = []
        for msg in new_msgs[-10:]:  # last 10 messages max for context
            context_lines.append(f"[{msg.sender_id}]: {msg.body}")
        context = "\n".join(context_lines)

        # Call LLM backend
        response_text = self._generate_response(channel_id, context)
        if not response_text:
            return

        # Post response
        try:
            gm.post_message(
                channel_id=channel_id,
                sender_id=self.team.agent_id,
                body=response_text[:self.config.max_response_length],
            )
            self._last_response[channel_id] = time.monotonic()
            logger.info("AgentDaemon %s responded in channel %s",
                        self.team.agent_id, channel_id)
        except Exception as e:
            logger.error("AgentDaemon %s failed to post response: %s",
                         self.team.agent_id, e)

    def _generate_response(self, channel_id: str, context: str) -> str:
        """Call the LLM backend to generate a response."""
        backend = self.team.backend
        if backend is None:
            logger.warning("AgentDaemon %s has no backend, cannot respond",
                           self.team.agent_id)
            return ""

        from team_layer.backends.base import SessionConfig

        system = self.config.system_prompt or (
            f"You are {self.team.agent_id}, a team member in an AI agent collaboration. "
            f"Respond concisely and helpfully to the conversation below."
        )
        prompt = f"Channel: #{channel_id}\n\nRecent messages:\n{context}\n\nYour response:"

        try:
            cfg = SessionConfig(
                session_id=f"daemon-{self.team.agent_id}-{int(time.time())}",
                goal=f"respond in channel {channel_id}",
                timeout=120,
                workdir=self.team.workspace,
            )
            backend.start_session(cfg)
            resp = backend.send_turn(prompt=prompt, system_prompt=system)
            backend.end_session()

            if resp.is_error:
                logger.error("AgentDaemon %s LLM error: %s",
                             self.team.agent_id, resp.error)
                return ""
            return resp.content.strip()
        except Exception as e:
            logger.error("AgentDaemon %s backend call failed: %s",
                         self.team.agent_id, e, exc_info=True)
            return ""
