"""
Blackboard   Agent /

Hearsay-II, 1970s Agent
 Agent


    shared               Git
    group:<name>         Git
    private:<agent_id>    Agent

Append-only JSON Lines PR 5 LogCollector
-  entry_id  get()
- hostname+timestamp
-
"""

from .scope import Scope, ScopeKind
from .blackboard import Blackboard, BlackboardEntry
from .views import render_kanban, render_table
from .provider import BlackboardProvider

__all__ = [
    "Scope",
    "ScopeKind",
    "Blackboard",
    "BlackboardEntry",
    "render_kanban",
    "render_table",
    "BlackboardProvider",
]
