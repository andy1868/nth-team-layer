"""
Channel — Agent 间带签名的消息通道（群聊/分组/私聊）

QQ/微信群的核心功能：Agents 可以在团队频道里发消息、@提及其他 Agent、
回复特定消息、轮询新消息。

设计：
- 消息持久化：team_messages/{channel_id}/*.jsonl（append-only，与 ledger 同模式）
- 零冲突命名：{hostname}_{agent_id}_{timestamp}.jsonl（git_sync 兼容）
- 三种 scope：
    - "team"         → 全团队频道（类似「全员群」）
    - "group:<name>" → 分组频道（类似「项目群」）
    - "dm:<a>_<b>"   → 私聊频道（agent_id 按字典序排列）
- 每条消息可选 Ed25519 签名（有 identity 时自动签名）
- 轮询模式（pull）：fetch(since=...) 拉取新消息
- @提及追踪：mentions 列表，可以查询「谁 @ 了我」

用法：
    team = nth.attach(identity=ident, ...)

    # 发群消息
    team.channel.send("大家好，今天谁值班？", scope="team")

    # 发给分组
    team.channel.send("后端接口更新了", scope="group:backend")

    # 私聊
    team.channel.dm("bob", "你的 PR 我看完了，LGTM!")

    # @提及
    team.channel.send("请 @alice 看看这个问题", mentions=["alice"])

    # 拉取新消息
    msgs = team.channel.fetch(since=last_checkpoint)
    for m in msgs:
        print(f"[{m.from_agent}] {m.content}")

    # 拉取所有频道的新消息（多频道聚合）
    all_msgs = team.channel.fetch_all(since=last_checkpoint)

    # 查谁 @ 了我
    mentions = team.channel.mentions_for(my_agent_id)

文件布局：
    team_messages/
    ├── team/
    │   ├── host1_alice_2026-05-27.jsonl
    │   └── host2_bob_2026-05-27.jsonl
    ├── group--backend/
    │   └── host1_alice_2026-05-27.jsonl
    └── dm--alice--bob/
        └── host1_alice_2026-05-27.jsonl
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .identity import AgentIdentity


# ─────────────────── 常量 ───────────────────

DEFAULT_MESSAGES_DIR = "team_messages"
TEAM_CHANNEL = "team"
DM_PREFIX = "dm"
GROUP_PREFIX = "group"


# ─────────────────── 数据模型 ───────────────────


@dataclass
class ChannelMessage:
    """一条频道消息"""

    msg_id: str
    channel: str           # "team" | "group:xxx" | "dm:alice--bob"
    from_agent: str        # agent_id
    content: str
    content_type: str = "text"  # "text" | "markdown" | "json"
    reply_to: str = ""     # 回复的 msg_id（空 = 顶级消息）
    mentions: List[str] = field(default_factory=list)  # @提及的 agent_id 列表
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sig: str = ""          # Ed25519 签名（128 字符 hex）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "channel": self.channel,
            "from_agent": self.from_agent,
            "content": self.content,
            "content_type": self.content_type,
            "reply_to": self.reply_to,
            "mentions": self.mentions,
            "timestamp": self.timestamp,
            "sig": self.sig,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelMessage":
        return cls(
            msg_id=data.get("msg_id", ""),
            channel=data.get("channel", TEAM_CHANNEL),
            from_agent=data.get("from_agent", "?"),
            content=data.get("content", ""),
            content_type=data.get("content_type", "text"),
            reply_to=data.get("reply_to", ""),
            mentions=data.get("mentions", []),
            timestamp=data.get("timestamp", ""),
            sig=data.get("sig", ""),
            metadata=data.get("metadata", {}),
        )

    @property
    def is_reply(self) -> bool:
        return bool(self.reply_to)

    @property
    def short_id(self) -> str:
        return self.msg_id[:8] if self.msg_id else "?"

    def __repr__(self) -> str:
        preview = self.content[:40].replace("\n", " ")
        return f"[{self.channel}] {self.from_agent}: {preview}"


# ─────────────────── 频道编码 ───────────────────


def _channel_dir(channel: str) -> str:
    """将频道名转为文件系统安全的目录名

    "team"      → "team"
    "group:backend" → "group--backend"
    "dm:alice--bob" → "dm--alice--bob"
    """
    return channel.replace(":", "--").replace("/", "-")


def _dm_channel(agent_a: str, agent_b: str) -> str:
    """生成 DM 频道名（两个 agent 按字典序排列，保证唯一）"""
    a, b = sorted([agent_a, agent_b])
    return f"{DM_PREFIX}:{a}--{b}"


# ─────────────────── TeamChannel ───────────────────


class TeamChannel:
    """Agent 间消息通道——群聊、分组、私聊"""

    # 每条消息最大长度（防止存储膨胀）
    MAX_CONTENT_LENGTH = 10000

    def __init__(
        self,
        workspace: Path,
        agent_id: str,
        identity: Optional[AgentIdentity] = None,
        messages_dir: str = DEFAULT_MESSAGES_DIR,
    ):
        """
        Args:
            workspace: 团队工作目录
            agent_id: 本 Agent 的 ID
            identity: 可选密码学身份（有则自动签名）
            messages_dir: 消息存储子目录名
        """
        self.workspace = workspace
        self.agent_id = agent_id
        self.identity = identity
        self.base_dir = workspace / messages_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ─────────── 发送 ───────────

    def send(
        self,
        content: str,
        scope: str = TEAM_CHANNEL,
        content_type: str = "text",
        reply_to: str = "",
        mentions: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChannelMessage:
        """发送消息到指定频道

        Args:
            content: 消息内容
            scope: "team" / "group:<name>" / "dm:<alice>--<bob>"
            content_type: "text" | "markdown" | "json"
            reply_to: 回复的消息 ID
            mentions: @提及的 agent_id 列表
            metadata: 附加元数据

        Returns:
            已创建的 ChannelMessage
        """
        # 截断过长消息
        if len(content) > self.MAX_CONTENT_LENGTH:
            content = content[: self.MAX_CONTENT_LENGTH - 50] + "\n... [truncated]"

        msg = ChannelMessage(
            msg_id=uuid.uuid4().hex,
            channel=scope,
            from_agent=self.agent_id,
            content=content,
            content_type=content_type,
            reply_to=reply_to,
            mentions=mentions or [],
            metadata=metadata or {},
        )

        # 如果有密码学身份 → 签名
        if self.identity and self.identity.can_sign:
            payload = {
                "msg_id": msg.msg_id,
                "channel": msg.channel,
                "from_agent": msg.from_agent,
                "content": msg.content,
                "content_type": msg.content_type,
                "reply_to": msg.reply_to,
                "mentions": msg.mentions,
                "timestamp": msg.timestamp,
                "metadata": msg.metadata,
            }
            msg.sig = self.identity.sign_json(payload)

        self._append(msg)
        return msg

    def dm(
        self,
        to_agent: str,
        content: str,
        content_type: str = "text",
    ) -> ChannelMessage:
        """发私聊消息"""
        channel = _dm_channel(self.agent_id, to_agent)
        return self.send(content=content, scope=channel, content_type=content_type)

    def reply(
        self,
        to_msg: ChannelMessage,
        content: str,
        mentions: Optional[List[str]] = None,
    ) -> ChannelMessage:
        """回复一条消息"""
        return self.send(
            content=content,
            scope=to_msg.channel,
            reply_to=to_msg.msg_id,
            mentions=mentions,
        )

    # ─────────── 读取 ───────────

    def fetch(
        self,
        channel: str = TEAM_CHANNEL,
        since: Optional[str] = None,      # ISO timestamp
        since_msg_id: Optional[str] = None,  # 或按消息 ID
        limit: int = 50,
    ) -> List[ChannelMessage]:
        """拉取指定频道的新消息（按时间顺序）

        Args:
            channel: 频道名
            since: 只返回此时间戳之后的消息
            since_msg_id: 只返回此消息 ID 之后的消息
            limit: 最大返回数

        Returns:
            消息列表（最早 → 最新）
        """
        dir_path = self.base_dir / _channel_dir(channel)
        if not dir_path.exists():
            return []

        messages = []
        found_since = since_msg_id is None and since is None

        for file_path in sorted(dir_path.glob("*.jsonl")):
            try:
                for line in file_path.read_text(encoding="utf-8").strip().split("\n"):
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = ChannelMessage.from_dict(data)

                    # since 过滤
                    if not found_since:
                        if since_msg_id and msg.msg_id == since_msg_id:
                            found_since = True
                            continue  # 跳过 since_msg_id 本身
                        if since and msg.timestamp > since:
                            found_since = True
                        if not found_since:
                            continue

                    messages.append(msg)

                    if len(messages) >= limit:
                        break
            except Exception:
                continue

        return messages[-limit:] if len(messages) > limit else messages

    def fetch_all(
        self,
        since: Optional[str] = None,
        limit_per_channel: int = 20,
        channels: Optional[List[str]] = None,
    ) -> Dict[str, List[ChannelMessage]]:
        """拉取所有频道的新消息（多频道聚合）

        Args:
            since: 时间戳过滤
            limit_per_channel: 每个频道最多返回数
            channels: 限制的频道列表（None = 所有已有频道）

        Returns:
            {channel_name: [messages]}
        """
        result = {}

        if channels:
            dirs = [_channel_dir(c) for c in channels]
        else:
            if not self.base_dir.exists():
                return result
            dirs = sorted(
                d.name for d in self.base_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )

        for dir_name in dirs:
            # 还原频道名
            channel = dir_name.replace("--", ":", 1)
            msgs = self.fetch(channel=channel, since=since, limit=limit_per_channel)
            if msgs:
                result[channel] = msgs

        return result

    def mentions_for(
        self,
        agent_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 30,
    ) -> List[ChannelMessage]:
        """查询 @提及本 Agent 的消息（未读提醒）

        Args:
            agent_id: 被 @ 的 agent（默认 = self.agent_id）
            since: 时间过滤
            limit: 最大返回数

        Returns:
            包含 @mention 的消息列表
        """
        target = agent_id or self.agent_id
        results = []

        if not self.base_dir.exists():
            return results

        for dir_path in sorted(self.base_dir.iterdir()):
            if not dir_path.is_dir() or dir_path.name.startswith("."):
                continue
            for file_path in sorted(dir_path.glob("*.jsonl")):
                try:
                    for line in file_path.read_text(encoding="utf-8").strip().split("\n"):
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        mentions = data.get("mentions", [])
                        if target not in mentions:
                            continue
                        if since and data.get("timestamp", "") <= since:
                            continue
                        results.append(ChannelMessage.from_dict(data))
                        if len(results) >= limit:
                            break
                except Exception:
                    continue

        return results

    # ─────────── 消息搜索 ───────────

    def search(
        self,
        keyword: str,
        channel: Optional[str] = None,
        from_agent: Optional[str] = None,
        limit: int = 20,
    ) -> List[ChannelMessage]:
        """关键词搜索消息

        Args:
            keyword: 搜索词（大小写不敏感）
            channel: 限制频道（None = 全部）
            from_agent: 限制发送者
            limit: 最大返回数
        """
        results = []
        keyword_lower = keyword.lower()

        dirs_to_search = (
            [self.base_dir / _channel_dir(channel)]
            if channel
            else [d for d in self.base_dir.iterdir() if d.is_dir()]
        )

        for dir_path in dirs_to_search:
            if not dir_path.exists():
                continue
            for file_path in sorted(dir_path.glob("*.jsonl")):
                try:
                    for line in file_path.read_text(encoding="utf-8").strip().split("\n"):
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if keyword_lower not in data.get("content", "").lower():
                            continue
                        if from_agent and data.get("from_agent") != from_agent:
                            continue
                        results.append(ChannelMessage.from_dict(data))
                        if len(results) >= limit:
                            return results
                except Exception:
                    continue

        return results

    # ─────────── 管理 ───────────

    def list_channels(self) -> List[str]:
        """列出所有已有频道"""
        if not self.base_dir.exists():
            return []
        return sorted(
            d.name.replace("--", ":", 1)
            for d in self.base_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    def stats(self) -> Dict[str, Any]:
        """频道统计"""
        total_messages = 0
        channels = {}

        if self.base_dir.exists():
            for dir_path in self.base_dir.iterdir():
                if not dir_path.is_dir() or dir_path.name.startswith("."):
                    continue
                count = 0
                for file_path in dir_path.glob("*.jsonl"):
                    try:
                        count += sum(1 for _ in file_path.read_text(encoding="utf-8").strip().split("\n") if _.strip())
                    except Exception:
                        pass
                channel_name = dir_path.name.replace("--", ":", 1)
                channels[channel_name] = count
                total_messages += count

        return {
            "total_messages": total_messages,
            "channels": channels,
            "channel_count": len(channels),
        }

    def cleanup(self, before: str) -> int:
        """清理指定时间之前的消息文件

        Args:
            before: ISO 时间戳字符串

        Returns:
            删除的文件数
        """
        removed = 0
        if not self.base_dir.exists():
            return 0

        for file_path in self.base_dir.rglob("*.jsonl"):
            try:
                # 文件名格式：{hostname}_{agent_id}_{timestamp}.jsonl
                # 取 timestamp 部分
                stem = file_path.stem
                parts = stem.rsplit("_", 1)
                if len(parts) == 2:
                    ts = parts[1]
                    if ts < before:
                        file_path.unlink()
                        removed += 1
            except Exception:
                continue
        return removed

    # ─────────── 内部 ───────────

    def _append(self, msg: ChannelMessage) -> None:
        """原子追加一条消息到 JSONL 文件"""
        channel_dir = self.base_dir / _channel_dir(msg.channel)
        channel_dir.mkdir(parents=True, exist_ok=True)

        # 零冲突命名：{hostname}_{agent_id}_{date}.jsonl
        date = msg.timestamp[:10]
        hostname = socket.gethostname().replace(".", "-")
        safe_agent = "".join(
            c if c.isalnum() or c in "_-" else "-" for c in msg.from_agent
        )
        filename = f"{hostname}_{safe_agent}_{date}.jsonl"
        file_path = channel_dir / filename

        line = json.dumps(msg.to_dict(), ensure_ascii=False) + "\n"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)
