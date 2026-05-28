"""
Gossip   P2P WebSocket gossip

 Agent  GossipNode WebSocket  agent
-  server client peer
- Agent      peer
- msg_id
-


- WebSocket (RFC 6455) Python asyncio
- websockets extra: pip install nth-dao[p2p]
-  channel.py gossip    channel
- NAT  localhost relay/tunnel

JSON over WebSocket
     {"type": "hello", "agent_id": "...", "pubkey_hex": "..."}
     {"type": "welcome", "agent_id": "...", "channels": [...]}
     {"type": "subscribe", "channel": "team"}
     {"type": "subscribed", "channel": "team"}
     {"type": "gossip", "message": {...ChannelMessage.to_dict()...}}
     {"type": "ack", "msg_id": "..."}
     {"type": "gossip", "message": {...}}  (from other peers)
     {"type": "peer_list"}  (request known peers)
     {"type": "peers", "peers": [{"agent_id": "...", "url": "..."}]}


    node = GossipNode(
        identity=my_identity,
        channel=my_channel,
        host="0.0.0.0",
        port=9876,
    )
    await node.start()

    #  peer
    await node.connect("ws://192.168.1.100:9876")

    #
    await node.subscribe("team")
    await node.subscribe("group:backend")

    #  gossip  peers +  channel
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

#

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
            f"  pip install nth-dao[p2p]\n"
            f"  (or: pip install websockets>=12.0)"
        )


#


@dataclass
class PeerInfo:
    """ peer """
    agent_id: str
    url: str           # ws://host:port
    pubkey_hex: str = ""
    channels: Set[str] = field(default_factory=set)
    connected_at: float = 0.0
    last_seen: float = 0.0

    @property
    def short_id(self) -> str:
        return self.agent_id[:8]


#  GossipNode


class GossipNode:
    """P2P Gossip """

    #
    DEDUP_CACHE_SIZE = 1000
    #
    PING_INTERVAL = 30
    #
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
            identity:
            channel:
            host:
            port:
            bootstrap_peers:  peer URL  ["ws://alice.local:9876"]
        """
        _ws_required("GossipNode")

        self.identity = identity
        self.channel = channel
        self.host = host
        self.port = port

        # Peer
        self.peers: Dict[str, websockets.WebSocketServerProtocol] = {}  # agent_id  connection
        self.peer_infos: Dict[str, PeerInfo] = {}  # agent_id  info
        self.bootstrap_urls = bootstrap_peers or []

        #
        self.subscriptions: Set[str] = set()

        # msg_id  timestamp
        self._seen: deque = deque(maxlen=self.DEDUP_CACHE_SIZE)

        #
        self._on_message: Optional[Callable] = None
        self._on_peer_join: Optional[Callable] = None
        self._on_peer_leave: Optional[Callable] = None

        #
        self._server = None
        self._running = False
        self._tasks: List[asyncio.Task] = []

    #

    def on_message(self, callback: Callable) -> None:
        """callback(msg_dict, from_agent_id)"""
        self._on_message = callback

    def on_peer_join(self, callback: Callable) -> None:
        """peer callback(peer_info)"""
        self._on_peer_join = callback

    def on_peer_leave(self, callback: Callable) -> None:
        """peer callback(agent_id)"""
        self._on_peer_leave = callback

    #

    async def start(self) -> str:
        """ gossip

        Returns:
             WebSocket URL
        """
        self._running = True
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
        )
        url = f"ws://{self.host}:{self.port}"

        #  bootstrap peers
        for peer_url in self.bootstrap_urls:
            asyncio.create_task(self._connect_with_retry(peer_url))

        #
        self._tasks.append(asyncio.create_task(self._ping_loop()))

        return url

    async def stop(self) -> None:
        """ gossip """
        self._running = False

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        #  peer
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

    #

    async def connect(self, url: str) -> bool:
        """ peer"""
        _ws_required("connect")
        try:
            ws = await ws_connect(url)
            #
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

                #
                self._tasks.append(asyncio.create_task(self._recv_loop(agent_id, ws)))

                #  peer
                await self._send_json(ws, {"type": "peer_list"})

                if self._on_peer_join:
                    self._on_peer_join(self.peer_infos[agent_id])

                return True
        except Exception:
            pass
        return False

    async def disconnect(self, agent_id: str) -> None:
        """ peer """
        ws = self.peers.pop(agent_id, None)
        self.peer_infos.pop(agent_id, None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        if self._on_peer_leave:
            self._on_peer_leave(agent_id)

    #

    async def subscribe(self, channel_name: str) -> None:
        """ peer"""
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

    #

    async def broadcast(
        self,
        content: str,
        scope: str = "team",
        content_type: str = "text",
        mentions: Optional[List[str]] = None,
    ) -> dict:
        """ channel + gossip  peer

        Returns:
             dict
        """
        #  channel
        msg = self.channel.send(
            content=content,
            scope=scope,
            content_type=content_type,
            mentions=mentions,
        )

        msg_dict = msg.to_dict()
        #  seen
        self._seen.append(msg_dict["msg_id"])

        # gossip  peer
        await self.gossip(msg_dict)

        return msg_dict

    async def gossip(self, msg_dict: dict) -> None:
        """ gossip  peer channel"""
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
        """ peer DM channel"""
        msg = self.channel.dm(to_agent, content, content_type)
        msg_dict = msg.to_dict()
        self._seen.append(msg_dict["msg_id"])

        #  peer
        if to_agent in self.peers:
            await self._send_json(self.peers[to_agent], {
                "type": "gossip",
                "message": msg_dict,
            })
            return msg_dict

        # peer    peer gossip
        await self.gossip(msg_dict)
        return msg_dict

    #

    def list_peers(self) -> List[PeerInfo]:
        """ peer"""
        return list(self.peer_infos.values())

    def peer_count(self) -> int:
        return len(self.peers)

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    #

    async def _handle_connection(self, ws):
        """ WebSocket """
        try:
            #
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(hello)

            if data.get("type") != "hello":
                await ws.close(1008, "expected hello")
                return

            remote_agent_id = data["agent_id"]
            remote_pubkey = data.get("pubkey_hex", "")

            #  welcome
            await self._send_json(ws, {
                "type": "welcome",
                "agent_id": str(self.identity.agent_id),
                "pubkey_hex": self.identity.pubkey_hex,
                "channels": list(self.subscriptions),
            })

            #  peer
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

            #
            await self._recv_loop(remote_agent_id, ws)

        except asyncio.TimeoutError:
            await ws.close(1008, "handshake timeout")
        except Exception:
            pass

    async def _recv_loop(self, agent_id: str, ws):
        """ peer """
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
                    #  peer
                    peers_list = [
                        {"agent_id": pid, "url": pi.url}
                        for pid, pi in self.peer_infos.items()
                    ]
                    await self._send_json(ws, {
                        "type": "peers",
                        "peers": peers_list,
                    })

                elif msg_type == "peers":
                    #  peer    peer
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
            # Peer
            await self.disconnect(agent_id)

    async def _handle_gossip(self, data: dict, from_agent_id: str) -> None:
        """ gossip  +  +  +  + """
        msg_dict = data.get("message", {})
        msg_id = msg_dict.get("msg_id", "")

        #
        if msg_id in self._seen:
            return
        self._seen.append(msg_id)

        #  pubkey
        sig = msg_dict.get("sig", "")
        if sig and from_agent_id in self.peer_infos:
            pubkey_hex = self.peer_infos[from_agent_id].pubkey_hex
            if pubkey_hex:
                #
                if not self.identity.verify_json(msg_dict, sig, pubkey_hex=pubkey_hex):
                    #
                    pass

        #  channelappend JSONL
        self._channel_append(msg_dict)

        #
        if self._on_message:
            try:
                self._on_message(msg_dict, from_agent_id)
            except Exception:
                pass

        #  peergossip fanout
        await self._relay(msg_dict, exclude=from_agent_id)

    async def _relay(self, msg_dict: dict, exclude: str = "") -> None:
        """ peer"""
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
        """ channel"""
        from .channel import ChannelMessage
        msg = ChannelMessage.from_dict(msg_dict)
        self.channel._append(msg)

    #

    async def _ping_loop(self) -> None:
        """ ping  peer"""
        while self._running:
            await asyncio.sleep(self.PING_INTERVAL)
            for agent_id, ws in list(self.peers.items()):
                try:
                    await self._send_json(ws, {"type": "ping"})
                except Exception:
                    await self.disconnect(agent_id)

    async def _connect_with_retry(self, url: str, max_retries: int = 10) -> None:
        """"""
        for i in range(max_retries):
            if not self._running:
                return
            if await self.connect(url):
                return
            await asyncio.sleep(self.RECONNECT_DELAY * (1 + i * 0.5))

    #

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
