"""
attach()   API

 Agent  NTH DAO

    import nth_dao as nth
    team = nth.attach(
        agent_id="my-agent",
        backend="mock",                #  AgentBackend
        capabilities=["python", "web"],
        groups=["frontend"],
        workspace="./my-team-workspace",
    )

    #
    team.memory                # TeamMemoryManager   system prompt
    team.blackboard            # Blackboard
    team.runner                # MissionRunner
    team.finder                # PeerFinder
    team.discover()            # list_alive agents
    team.start_mission(...)
    team.detach()              #


- attach() 4 Provider + Blackboard + Discovery + Mission
- TeamSession  facade
- detach() ledger
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from team_layer import TeamAgent, TeamMemoryManager
from team_layer.backends import AgentBackend, default_registry
from team_layer.blackboard import Blackboard, BlackboardProvider
from team_layer.memory_providers import (
    LedgerProvider,
    SoulProvider,
    UserModelProvider,
    VectorProvider,
)

from .discovery import AgentRegistry, PeerFinder
from .orchestration import Mission, MissionRunner, MissionStore
from .membership import MembershipManager
from .identity import AgentIdentity


@dataclass
class TeamSession:
    """attach()  facade    NTH DAO runtime """
    agent_id: str
    backend_id: str
    workspace: Path

    agent: TeamAgent
    memory: TeamMemoryManager
    blackboard: Blackboard
    registry: AgentRegistry
    finder: PeerFinder
    mission_store: MissionStore
    runner: MissionRunner
    membership: MembershipManager
    identity: Optional[AgentIdentity] = None
    backend: Optional[AgentBackend] = None
    capabilities: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    _detached: bool = False

    #

    def discover(self) -> List:
        """ Agent"""
        return self.registry.list_alive()

    def discover_others(self) -> List:
        """ Agent"""
        return [r for r in self.registry.list_alive() if r.agent_id != self.agent_id]

    def find_teammate(
        self,
        capability: Optional[str] = None,
        needed_capabilities: Optional[List[str]] = None,
        group: Optional[str] = None,
    ):
        """"""
        if needed_capabilities:
            return self.finder.best_match(
                needed_capabilities=needed_capabilities,
                prefer_idle=True,
                exclude_agent_ids=[self.agent_id],
            )
        if capability:
            results = self.finder.find(
                capability=capability,
                exclude_agent_ids=[self.agent_id],
            )
            return results[0] if results else None
        if group:
            results = self.finder.find(
                group=group,
                exclude_agent_ids=[self.agent_id],
            )
            return results[0] if results else None
        return None

    def start_mission(
        self,
        title: str,
        goal: str,
        steps: List[dict],
        scope: str = "shared",
        deadline: Optional[str] = None,
        priority: str = "normal",
        tags: Optional[List[str]] = None,
    ) -> Mission:
        """"""
        m = Mission.new(
            title=title,
            goal=goal,
            owner=self.agent_id,
            scope=scope,
            steps=steps,
            deadline=deadline,
            priority=priority,
            tags=tags or [],
        )
        self.mission_store.create(m)

        #  mission  blackboard Kanban
        self.blackboard.post(
            topic=f"[MISSION] {title}",
            author=self.agent_id,
            scope=scope,
            status="doing",
            content=goal,
            metadata={"mission_id": m.id, "type": "mission"},
        )

        #
        self.registry.update_status(current_mission=m.id)

        return m

    def take_next_work(self) -> Optional[Mission]:
        """ step  claim  """""
        found = self.runner.find_work()
        if not found:
            return None
        mission, step = found
        self.runner.claim(mission.id, step.id)
        self.registry.update_status(status="busy", current_mission=mission.id)
        return mission

    def detach(self) -> None:
        """ + """
        if self._detached:
            return
        #  agent
        try:
            self.agent.finalize()
        except Exception as e:
            print(f"[ATTACH] finalize warning: {e}")
        #
        try:
            self.registry.unregister()
        except Exception as e:
            print(f"[ATTACH] unregister warning: {e}")
        self._detached = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.detach()


#
# attach()
#

def attach(
    agent_id: str,
    backend: Union[str, AgentBackend, None] = None,
    backend_kwargs: Optional[Dict[str, Any]] = None,
    capabilities: Optional[List[str]] = None,
    groups: Optional[List[str]] = None,
    workspace: Union[str, Path] = ".",
    metadata: Optional[Dict[str, Any]] = None,
    soul_path: str = "skills/TEAM-SOUL.md",
    user_model_path: str = "memory/user-model.json",
    vector_dir: str = "skills/registry",
    ledger_path: str = "sidechain/ledger.jsonl",
    blackboard_root: str = "blackboard",
    agents_dir: str = "team_agents",
    missions_dir: str = "missions",
    compression_threshold: float = 0.75,
    start_heartbeat: bool = True,
    join_token: str = "",
    identity: Optional[AgentIdentity] = None,
) -> TeamSession:
    """
     NTH DAO

    Args:
        agent_id:  Agent id
        backend:  registry  AgentBackend  None backend
        backend_kwargs:  backend  ctor
        capabilities:  ["python", "web", "codegen"]
        groups:  ["frontend", "ops"]
        workspace:  dir
         NTH DAO runtime

    Returns:
        TeamSession   facade  with  detach
    """
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    capabilities = capabilities or []
    groups = groups or []
    membership = MembershipManager(workspace)
    agent_identity = identity or AgentIdentity.from_string(agent_id, label=agent_id)

    # 1. backend
    backend_instance: Optional[AgentBackend] = None
    backend_id_str = "none"
    if isinstance(backend, AgentBackend):
        backend_instance = backend
        backend_id_str = backend.backend_id
    elif isinstance(backend, str):
        backend_instance = default_registry.create(backend, **(backend_kwargs or {}))
        backend_id_str = backend

    allowed, reason = membership.ensure_member(agent_id, token=join_token)
    if not allowed:
        raise PermissionError(
            f"Agent '{agent_id}' cannot attach to this team: {reason}. "
            "Submit a join request or ask a team admin for approval/invite."
        )

    # 2. 4+1  Provider
    providers = [
        SoulProvider(str(workspace / soul_path)),
        UserModelProvider(str(workspace / user_model_path)),
        VectorProvider(str(workspace / vector_dir)),
        LedgerProvider(str(workspace / ledger_path)),
        BlackboardProvider(
            agent_id=agent_id,
            blackboard_root=str(workspace / blackboard_root),
            groups=groups,
        ),
    ]

    # 3. TeamMemoryManager + TeamAgent
    mem = TeamMemoryManager(providers, session_id=f"{agent_id}_session")
    mem.initialize({"agent_id": agent_id, "capabilities": capabilities})
    team_agent = TeamAgent(
        agent_id=agent_id,
        team_memory_manager=mem,
        compression_threshold=compression_threshold,
    )

    # 4. Blackboard
    blackboard = Blackboard(workspace / blackboard_root)

    # 5. Discovery   +
    registry = AgentRegistry(agents_dir=str(workspace / agents_dir))
    registry_metadata = dict(metadata or {})
    registry_metadata.setdefault("identity", agent_identity.public_dict())

    registry.register(
        agent_id=agent_id,
        backend_id=backend_id_str,
        capabilities=capabilities,
        groups=groups,
        metadata=registry_metadata,
        start_heartbeat=start_heartbeat,
    )

    # 6. PeerFinder
    finder = PeerFinder(registry)

    # 7. Mission Orchestration
    mission_store = MissionStore(str(workspace / missions_dir))
    runner = MissionRunner(
        store=mission_store,
        agent_id=agent_id,
        capabilities=capabilities,
    )

    return TeamSession(
        agent_id=agent_id,
        backend_id=backend_id_str,
        workspace=workspace,
        agent=team_agent,
        memory=mem,
        blackboard=blackboard,
        registry=registry,
        finder=finder,
        mission_store=mission_store,
        runner=runner,
        membership=membership,
        identity=agent_identity,
        backend=backend_instance,
        capabilities=capabilities,
        groups=groups,
    )
