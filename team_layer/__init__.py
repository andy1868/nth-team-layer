"""Internal runtime helpers for NTH DAO.

The public protocol surface is `nth_dao`. This package keeps older runtime
building blocks such as memory providers, blackboard storage, backend adapters,
compression, evolution, and Git sync helpers.
"""

__version__ = "1.0.0"
__author__ = "NTH DAO"

from .runtime import TeamAgent, TeamMemoryManager

__all__ = ["TeamAgent", "TeamMemoryManager"]
