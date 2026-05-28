"""
Agent

 nth_dao  Agent


1. AgentRegistry   Agent
                    last_seen
                   Git
2. PeerFinder      capability / backend / status / scope
                    Agent
"""

from .agent_registry import AgentRegistry, AgentRecord
from .peer_finder import PeerFinder

__all__ = ["AgentRegistry", "AgentRecord", "PeerFinder"]
