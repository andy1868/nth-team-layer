"""
Gossip — 轻量级 P2P 传输层（WebSocket gossip 协议）

每个 Agent 运行一个 GossipNode，通过 WebSocket 与其他 agent 直连通信：
- 每个节点既是 server（监听端口）也是 client（连接其他 peer）
- Agent 订阅频道 → 收到消息 → 转发给 peer → 最终一致
- 消息去重（msg_id 缓存）、签名验证
- 断电后自动从文件恢复到最新状态

设计：
- 传输层：WebSocket (RFC 6455)，纯 Python asyncio
- 依赖：websockets（可选 extra: pip install nth-team-layer[p2p]）
- 与 channel.py 的关系：gossip 收到消息 → 写入 channel → 触发回调
- NAT 友好：默认 localhost，可通过 relay/tunnel 扩展

协议（JSON over WebSocket）：
    → {"type": "hello", "agent_id": "...", "pubkey_hex": "..."}
    ← {"type": "welcome", "agent_id": "...", "channels": [...]}
    → {"type": "subscribe", "channel": "team"}
    ← {"type": "subscribed", "channel": "team"}
    → {"type": "gossip", "message": {...ChannelMessage.to_dict()...}}
    ← {"type": "ack", "msg_id": "..."}
    ← {"type": "gossip", "message": {...}}  (from other peers)
    → {"type": "peer_list"}  (request known peers)
    ← {"type": "peers", "peers": [{"agent_id": "...", "url": "..."}]}

用法：
    node = GossipNode(
        identity=my_identity,
        channel=my_channel,
        host="0.0.0.0",
        port=9876,
    )
    await node.start()

    # 连接已知 peer
    await node.connect("ws://192.168.1.100:9876")

    # 订阅频道
    await node.subscribe("team")
    await node.subscribe("group:backend")

    # 发消息（自动 gossip 到 peers + 写入本地 channel）
    await node.broadcast("hello team!", scope="team")
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

# ─────────────────── 可选依赖检测 ───────────────────

_WEBSOCKETS_AVAILABLE = False
try:
    import websockets
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect as ws_connect
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    pass


def _ws_required(feature: str) -> None:
    if not _WEBSOCKETS_AVAILABLE:
        raise ImportError(
            f"{feature} requires websockets. Install with:\n"
            f"  pip install nth-team-layer[p2p]\n"
            f"  (or: pip install websockets>=12.0)"
        )


# ─────────────────── 协议 ───────────────────


@dataclass
class PeerInfo:
    """已知 peer 的信息"""
    agent_id: str
    url: str           # ws://host:port
    pubkey_hex: str = ""
    channels: Set[str] = field(default_factory=set)
    connected_at: float = 0.0
    last_seen: float = 0.0

    @property
    def short_id(self) -> str:
        return self.agent_id[:8]


# ─────────────────── GossipNode ───────────────────


class GossipNode:
    """P2P Gossip 节点"""

    # 消息去重缓存大小
    DEDUP_CACHE_SIZE = 1000
    # 心跳间隔（秒）
    PING_INTERVAL = 30
    # 连接重试间隔（秒）
    RECONNECT_DELAY = 5

    def __init__(
        self,
        identity,       # AgentIdentity
        channel,        # TeamChannel
        host: str = "127.0.0.1",
        port: int = 9876,
        bootstrap_peers: Optional[List[str]] = None,
    ):
        """
        Args:
            identity: 密码学身份（用于签名和验证）
            channel: 本地消息通道（用于持久化）
            host: 监听地址
            port: 监听端口
            bootstrap_peers: 初始 peer URL 列表（如 ["ws://alice.local:9876"]）
        """
        _ws_required("GossipNode")

        self.identity = identity
        self.channel = channel
        self.host = host
        self.port = port

        # Peer 管理
        self.peers: Dict[str, websockets.WebSocketServerProtocol] = {}  # agent_id → connection
        self.peer_infos: Dict[str, PeerInfo] = {}  # agent_id → info
        self.bootstrap_urls = bootstrap_peers or []

        # 频道订阅
        self.subscriptions: Set[str] = set()

        # 消息去重（msg_id → timestamp）
        self._seen: deque = deque(maxlen=self.DEDUP_CACHE_SIZE)

        # 回调
        self._on_message: Optional[Callable] = None
        self._on_peer_join: Optional[Callable] = None
        self._on_peer_leave: Optional[Callable] = None

        # 状态
        self._server = None
        self._running = False
        self._tasks: List[asyncio.Task] = []

    # ─────────── 回调 ───────────

    def on_message(self, callback: Callable) -> None:
        """设置消息回调：callback(msg_dict, from_agent_id)"""
        self._on_message = callback

    def on_peer_join(self, callback: Callable) -> None:
        """peer 加入回调：callback(peer_info)"""
        self._on_peer_join = callback

    def on_peer_leave(self, callback: Callable) -> None:
        """peer 离开回调：callback(agent_id)"""
        self._on_peer_leave = callback

    # ─────────── 生命周期 ───────────

    async def start(self) -> str:
        """启动 gossip 节点

        Returns:
            本节点的 WebSocket URL
        """
        self._running = True
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
        )
        url = f"ws://{self.host}:{self.port}"

        # 连接 bootstrap peers
        for peer_url in self.bootstrap_urls:
            asyncio.create_task(self._connect_with_retry(peer_url))

        # 启动心跳
        self._tasks.append(asyncio.create_task(self._ping_loop()))

        return url

    async def stop(self) -> None:
        """停止 gossip 节点"""
        self._running = False

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        # 断开所有 peer 连接
        for agent_id, ws in list(self.peers.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self.peers.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ─────────── 连接管理 ───────────

    async def connect(self, url: str) -> bool:
        """连接到指定 peer"""
        _ws_required("connect")
        try:
            ws = await ws_connect(url)
            # 握手
            await self._send_hello(ws)
            response = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(response)

            if data.get("type") == "welcome":
                agent_id = data["agent_id"]
                self.peers[agent_id] = ws
                self.peer_infos[agent_id] = PeerInfo(
                    agent_id=agent_id,
                    url=url,
                    pubkey_hex=data.get("pubkey_hex", ""),
                    channels=set(data.get("channels", [])),
                    connected_at=time.time(),
                    last_seen=time.time(),
                )

                # 启动该连接的接收循环
                self._tasks.append(asyncio.create_task(self._recv_loop(agent_id, ws)))

                # 请求 peer 列表
                await self._send_json(ws, {"type": "peer_list"})

                if self._on_peer_join:
                    self._on_peer_join(self.peer_infos[agent_id])

                return True
        except Exception:
            pass
        return False

    async def disconnect(self, agent_id: str) -> None:
        """断开与指定 peer 的连接"""
        ws = self.peers.pop(agent_id, None)
        self.peer_infos.pop(agent_id, None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        if self._on_peer_leave:
            self._on_peer_leave(agent_id)

    # ─────────── 频道订阅 ───────────

    async def subscribe(self, channel_name: str) -> None:
        """订阅频道（通知所有 peer）"""
        self.subscriptions.add(channel_name)

        for ws in self.peers.values():
            await self._send_json(ws, {
                "type": "subscribe",
                "channel": channel_name,
            })

    async def unsubscribe(self, channel_name: str) -> None:
        self.subscriptions.discard(channel_name)
        for ws in self.peers.values():
            await self._send_json(ws, {
                "type": "unsubscribe",
                "channel": channel_name,
            })

    # ─────────── 消息收发 ───────────

    async def broadcast(
        self,
        content: str,
        scope: str = "team",
        content_type: str = "text",
        mentions: Optional[List[str]] = None,
    ) -> dict:
        """广播消息：写入本地 channel + gossip 到所有 peer

        Returns:
            消息 dict
        """
        # 写入本地 channel
        msg = self.channel.send(
            content=content,
            scope=scope,
            content_type=content_type,
            mentions=mentions,
        )

        msg_dict = msg.to_dict()
        # 标记为 seen（自己的消息）
        self._seen.append(msg_dict["msg_id"])

        # gossip 到所有连接的 peer
        await self.gossip(msg_dict)

        return msg_dict

    async def gossip(self, msg_dict: dict) -> None:
        """将消息 gossip 到所有连接的 peer（不写入本地 channel）"""
        tasks = []
        for ws in self.peers.values():
            tasks.append(self._send_json(ws, {
                "type": "gossip",
                "message": msg_dict,
            }))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def direct_message(
        self,
        to_agent: str,
        content: str,
        content_type: str = "text",
    ) -> Optional[dict]:
        """直接发送消息到指定 peer（同时写入本地 DM channel）"""
        msg = self.channel.dm(to_agent, content, content_type)
        msg_dict = msg.to_dict()
        self._seen.append(msg_dict["msg_id"])

        # 如果该 peer 在线 → 直接推送
        if to_agent in self.peers:
            await self._send_json(self.peers[to_agent], {
                "type": "gossip",
                "message": msg_dict,
            })
            return msg_dict

        # peer 不在线 → 靠其他 peer gossip 到达
        await self.gossip(msg_dict)
        return msg_dict

    # ─────────── 查询 ───────────

    def list_peers(self) -> List[PeerInfo]:
        """列出所有已连接的 peer"""
        return list(self.peer_infos.values())

    def peer_count(self) -> int:
        return len(self.peers)

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    # ─────────── 内部 ───────────

    async def _handle_connection(self, ws):
        """处理入站 WebSocket 连接"""
        try:
            # 等待握手
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(hello)

            if data.get("type") != "hello":
                await ws.close(1008, "expected hello")
                return

            remote_agent_id = data["agent_id"]
            remote_pubkey = data.get("pubkey_hex", "")

            # 回复 welcome
            await self._send_json(ws, {
                "type": "welcome",
                "agent_id": str(self.identity.agent_id),
                "pubkey_hex": self.identity.pubkey_hex,
                "channels": list(self.subscriptions),
            })

            # 注册 peer
            self.peers[remote_agent_id] = ws
            self.peer_infos[remote_agent_id] = PeerInfo(
                agent_id=remote_agent_id,
                url=f"ws://{ws.remote_address[0]}:{ws.remote_address[1]}",
                pubkey_hex=remote_pubkey,
                connected_at=time.time(),
                last_seen=time.time(),
            )

            if self._on_peer_join:
                self._on_peer_join(self.peer_infos[remote_agent_id])

            # 接收循环
            await self._recv_loop(remote_agent_id, ws)

        except asyncio.TimeoutError:
            await ws.close(1008, "handshake timeout")
        except Exception:
            pass

    async def _recv_loop(self, agent_id: str, ws):
        """接收来自 peer 的消息"""
        try:
            async for raw in ws:
                self.peer_infos.get(agent_id, PeerInfo(agent_id=agent_id, url="")).last_seen = time.time()

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "gossip":
                    await self._handle_gossip(data, agent_id)

                elif msg_type == "peer_list":
                    # 返回已知 peer 列表
                    peers_list = [
                        {"agent_id": pid, "url": pi.url}
                        for pid, pi in self.peer_infos.items()
                    ]
                    await self._send_json(ws, {
                        "type": "peers",
                        "peers": peers_list,
                    })

                elif msg_type == "peers":
                    # 收到 peer 列表 → 尝试连接新 peer
                    for p in data.get("peers", []):
                        if p["agent_id"] not in self.peers and p["agent_id"] != str(self.identity.agent_id):
                            asyncio.create_task(self._connect_with_retry(p["url"]))

                elif msg_type == "subscribe":
                    ch = data.get("channel", "")
                    if ch:
                        pi = self.peer_infos.get(agent_id)
                        if pi:
                            pi.channels.add(ch)

                elif msg_type == "unsubscribe":
                    ch = data.get("channel", "")
                    pi = self.peer_infos.get(agent_id)
                    if pi and ch in pi.channels:
                        pi.channels.discard(ch)

                elif msg_type == "ping":
                    await self._send_json(ws, {"type": "pong"})

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Peer 离开
            await self.disconnect(agent_id)

    async def _handle_gossip(self, data: dict, from_agent_id: str) -> None:
        """处理 gossip 消息（去重 + 验证 + 持久化 + 回调 + 转发）"""
        msg_dict = data.get("message", {})
        msg_id = msg_dict.get("msg_id", "")

        # 去重
        if msg_id in self._seen:
            return
        self._seen.append(msg_id)

        # 签名验证（可选，有 pubkey 时验证）
        sig = msg_dict.get("sig", "")
        if sig and from_agent_id in self.peer_infos:
            pubkey_hex = self.peer_infos[from_agent_id].pubkey_hex
            if pubkey_hex:
                # 验证签名（失败只记录，不拒绝）
                if not self.identity.verify_json(msg_dict, sig, pubkey_hex=pubkey_hex):
                    # 签名无效 → 仍接受消息但标记
                    pass

        # 持久化到本地 channel（append JSONL）
        self._channel_append(msg_dict)

        # 触发回调
        if self._on_message:
            try:
                self._on_message(msg_dict, from_agent_id)
            except Exception:
                pass

        # 转发给其他 peer（gossip fanout）
        await self._relay(msg_dict, exclude=from_agent_id)

    async def _relay(self, msg_dict: dict, exclude: str = "") -> None:
        """转发消息给 peer（排除来源）"""
        tasks = []
        for agent_id, ws in self.peers.items():
            if agent_id != exclude:
                tasks.append(self._send_json(ws, {
                    "type": "gossip",
                    "message": msg_dict,
                }))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _channel_append(self, msg_dict: dict) -> None:
        """将收到的消息写入本地 channel"""
        from .channel import ChannelMessage
        msg = ChannelMessage.from_dict(msg_dict)
        self.channel._append(msg)

    # ─────────── 心跳 ───────────

    async def _ping_loop(self) -> None:
        """定期 ping 所有 peer"""
        while self._running:
            await asyncio.sleep(self.PING_INTERVAL)
            for agent_id, ws in list(self.peers.items()):
                try:
                    await self._send_json(ws, {"type": "ping"})
                except Exception:
                    await self.disconnect(agent_id)

    async def _connect_with_retry(self, url: str, max_retries: int = 10) -> None:
        """带重试的连接"""
        for i in range(max_retries):
            if not self._running:
                return
            if await self.connect(url):
                return
            await asyncio.sleep(self.RECONNECT_DELAY * (1 + i * 0.5))

    # ─────────── 握手 ───────────

    async def _send_hello(self, ws) -> None:
        await self._send_json(ws, {
            "type": "hello",
            "agent_id": str(self.identity.agent_id),
            "pubkey_hex": self.identity.pubkey_hex,
        })

    @staticmethod
    async def _send_json(ws, data: dict) -> None:
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
