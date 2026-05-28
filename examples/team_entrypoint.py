"""
team_entrypoint.py  NTH DAO Agent PR 1-5


    []
          --reload-skills  SkillLoader.reload()
          sidechain/reload.signal
    [ TeamAgent]
         TeamMemoryManager(SoulProvider, UserModelProvider, VectorProvider, LedgerProvider)
          system prompt with memory fence
    []
         context usage   CompressionPipeline.auto_compress (5 )
          reload.signal
         append_history + LedgerProvider.record
    []
         agent.finalize()   user model
          --auto-collect  LogCollector.collect   team_logs/
          --auto-evolve  EvoLoop.run_once  AUTO_MERGE/PENDING_REVIEW


    #  PR 1-3
    python team_entrypoint.py --goal "" --agent nlp-1

    #
    python team_entrypoint.py --goal "..." --reload-skills

    #  collect + evolve
    python team_entrypoint.py --goal "..." --auto-collect --auto-evolve --no-push

    #  +  team_logs/
    python team_entrypoint.py --goal "..." --auto-collect --auto-evolve
"""

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

# Windows  stdout/stderr UTF-8 GBK
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/ -> repo root

from team_layer import TeamAgent, TeamMemoryManager
from team_layer.backends import BackendUnavailableError, default_registry
from team_layer.compression import CompressionPipeline
from team_layer.evolution import EvoLoop
from team_layer.git_sync import LogCollector, SkillLoader, SyncConfig
from team_layer.memory_providers import (
    LedgerProvider,
    SoulProvider,
    UserModelProvider,
    VectorProvider,
)


#
#  1:
#

def startup_hook(args, cfg: SyncConfig) -> None:
    """
     reload  +

     PR 5 (SkillLoader)   Agent
    """
    # 1.  reload.signal
    pending = SkillLoader.check_reload_pending(cfg)
    if pending:
        print(f"[STARTUP] Pending reload signal detected from {pending.get('hostname')}")
        print(f"[STARTUP]   Paths to reload: {pending.get('reload_paths')}")
        # check  unlink

    # 2.
    if args.reload_skills:
        print("[STARTUP] --reload-skills enabled, fetching latest skills...")
        try:
            loader = SkillLoader(cfg)
            result = loader.reload(send_signal=False)  #  Agent
            print(f"[STARTUP]   {result}")
        except Exception as e:
            print(f"[STARTUP] reload failed (non-fatal): {e}")


#
#  2:  Agent
#

def create_team_context(goal: str, agent_id: str, compression_threshold: float = 0.75) -> TeamAgent:
    """ TeamAgent PR 1+2"""
    providers = [
        SoulProvider("skills/TEAM-SOUL.md"),
        UserModelProvider("memory/user-model.json"),
        VectorProvider("skills/registry"),
        LedgerProvider("sidechain/ledger.jsonl"),
    ]

    session_id = f"{agent_id}_{goal.replace(' ', '_')[:40]}"
    mem_mgr = TeamMemoryManager(providers, session_id=session_id)
    mem_mgr.initialize({"goal": goal, "agent_id": agent_id})

    return TeamAgent(
        agent_id=agent_id,
        team_memory_manager=mem_mgr,
        compression_threshold=compression_threshold,
    )


#
#  3:
#

def per_iteration_hook(agent: TeamAgent, iteration: int, cfg: SyncConfig) -> None:
    """
     +

     PR 3 (CompressionPipeline) + PR 5 (SkillLoader runtime reload)
    """
    # 1. PR 3
    if agent.should_compact():
        agent.trigger_compression()
        pipeline = CompressionPipeline(
            history=agent.history,
            effort_level="high",
        )
        msg = pipeline.auto_compress(threshold=agent.compression_threshold)
        print(f"[ITER {iteration}] {msg}")

    # 2. PR 5  5
    if iteration > 0 and iteration % 5 == 0:
        pending = SkillLoader.check_reload_pending(cfg)
        if pending:
            print(f"[ITER {iteration}] Runtime reload signal from {pending.get('hostname')}")
            #  SoulProvider/VectorProvider
            for name in ("SoulProvider", "VectorProvider"):
                provider = agent.team_mem.providers.get(name)
                if provider:
                    try:
                        provider.initialize({})
                        print(f"[ITER {iteration}]   reloaded {name}")
                    except Exception as e:
                        print(f"[ITER {iteration}]   {name} reload failed: {e}")


#
#  4:
#

def shutdown_hook(agent: TeamAgent, args, cfg: SyncConfig) -> None:
    """
     +  collect +  evolve

     PR 5 (LogCollector) + PR 4 (EvoLoop)
    """
    # 1. PR 2  user model ledger
    agent.finalize()

    # 2.  collectPR 5
    if args.auto_collect:
        print("\n[SHUTDOWN] --auto-collect: exporting session logs...")
        try:
            collector = LogCollector(cfg)
            result = collector.collect(auto_push=not args.no_push)
            print(f"[SHUTDOWN]   {result}")
        except Exception as e:
            print(f"[SHUTDOWN] collect failed (non-fatal): {e}")

    # 3.  evolvePR 4
    if args.auto_evolve:
        print("\n[SHUTDOWN] --auto-evolve: running EvoLoop on local ledger...")
        try:
            ledger = agent.team_mem.providers.get("LedgerProvider")
            if not ledger:
                print("[SHUTDOWN] no LedgerProvider, skipping evolve")
                return
            loop = EvoLoop(ledger=ledger)
            results = loop.run_once()
            if not results:
                print("[SHUTDOWN]   no signatures met ROI threshold")
            else:
                for r in results:
                    print(f"[SHUTDOWN]   {r.summary()}")
        except Exception as e:
            print(f"[SHUTDOWN] evolve failed (non-fatal): {e}")


#
#
#

def run_agent_loop_with_backend(
    agent: TeamAgent,
    backend_id: str,
    goal: str,
    max_iterations: int,
    cfg: SyncConfig,
    backend_kwargs: Optional[dict] = None,
) -> None:
    """
    PR 7: backend-driven

     AgentBackendmock / hermes / claude_code / openclaw / codex / openhands
    """
    print(f"\n{'='*60}")
    print(f"Team Agent: {agent.agent_id}")
    print(f"Backend:    {backend_id}")
    print(f"Goal:       {goal}")
    print(f"Session:    {agent.session_id}")
    print(f"{'='*60}\n")

    try:
        backend = default_registry.create(backend_id, **(backend_kwargs or {}))
    except BackendUnavailableError as e:
        print(f"[ERROR] Backend '{backend_id}' unavailable: {e}")
        print(f"[ERROR] Available now: {default_registry.list_available(refresh=True)}")
        raise

    result = agent.run_with_backend(
        backend=backend,
        goal=goal,
        max_turns=max_iterations,
    )

    print(f"\n Backend session done  {result['session_summary'].total_turns} turns, "
          f"{result['session_summary'].total_usage.total} tokens")


def run_agent_loop(agent: TeamAgent, goal: str, max_iterations: int, cfg: SyncConfig) -> None:
    """
    mock    Hermes Agent.run()


        1.  per_iteration_hook +  reload
        2. mock  LLM  +
        3. append_history +
    """
    print(f"\n{'='*60}")
    print(f"Team Agent: {agent.agent_id}")
    print(f"Goal: {goal}")
    print(f"Session: {agent.session_id}")
    print(f"{'='*60}\n")

    system_prompt = agent.get_system_prompt_with_memory(
        base_prompt="You are a helpful AI assistant working in a team environment."
    )
    print("[SYSTEM PROMPT (preview)]")
    print(system_prompt[:500] + ("..." if len(system_prompt) > 500 else ""))
    print()

    for iteration in range(max_iterations):
        print(f"\n--- Iteration {iteration + 1} ---")
        print(f"Context usage: {agent.context_usage:.1%}")

        # PR 3 + PR 5
        per_iteration_hook(agent, iteration + 1, cfg)

        # mock LLM  +
        action = {"type": "think", "content": f"Working on: {goal}"}
        result = f"Progress: iteration {iteration + 1}"

        agent.append_history(action, result)

        #  EvoLoop
        agent.team_mem.providers["LedgerProvider"].record(
            agent_id=agent.agent_id,
            action_type="think",
            result=result,
            error_sig=None,
            token_cost=100,
        )

    print(f"\n Completed {max_iterations} iterations")


#
# CLI
#

def main():
    parser = argparse.ArgumentParser(
        description="NTH DAO Agent  NTH DAO ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
 PR 1-5
  PR 1: TeamAgent
  PR 2: 4  Provider (Soul/User/Vector/Ledger)
  PR 3: 5
  PR 4: EvoLoop --auto-evolve
  PR 5: --auto-collect / --reload-skills

Examples:
  #
  python team_entrypoint.py --goal ""

  #
  python team_entrypoint.py --goal "..." --reload-skills

  #
  python team_entrypoint.py --goal "..." --auto-collect --auto-evolve --reload-skills
        """,
    )

    #
    parser.add_argument("--goal", type=str, help="Agent  ( --list-backends )")
    parser.add_argument("--agent", type=str, default="team-agent-1", help="Agent ID")
    parser.add_argument("--iterations", type=int, default=5, help="")
    parser.add_argument("--compression-threshold", type=float, default=0.75,
                        help=" (0.0-1.0,  0.75)")

    # PR 7: Backend
    parser.add_argument("--backend", type=str, default=None,
                        choices=["mock", "hermes", "claude_code", "openclaw", "codex", "openhands"],
                        help="Agent backend (:  mock )")
    parser.add_argument("--backend-config", type=str, default=None,
                        help="JSON  backend ")
    parser.add_argument("--list-backends", action="store_true",
                        help=" backend ")

    # PR 5  flag
    parser.add_argument("--reload-skills", action="store_true",
                        help=" ( SkillLoader.reload)")
    parser.add_argument("--auto-collect", action="store_true",
                        help=" collect  team_logs/")
    parser.add_argument("--no-push", action="store_true",
                        help=" git pushauto-collect  commit  push")

    # PR 4  flag
    parser.add_argument("--auto-evolve", action="store_true",
                        help=" EvoLoop")

    args = parser.parse_args()

    #  PR 7: --list-backends
    if args.list_backends:
        desc = default_registry.describe(refresh=True)
        print("Registered backends:")
        for bid, info in desc.items():
            status = " AVAILABLE  " if info["available"] else " unavailable"
            note = info["capabilities"].get("notes", "")
            print(f"  {bid:15s} {status}  {note}")
            if info.get("error"):
                print(f"     error: {info['error']}")
        sys.exit(0)

    if not args.goal:
        parser.error("--goal is required (unless --list-backends)")

    # auto_push  --no-push
    cfg = SyncConfig(auto_push=not args.no_push)
    print(f"[CONFIG] {cfg.describe()}")
    print(f"[CONFIG] backend={args.backend or 'built-in mock loop'}, "
          f"auto_collect={args.auto_collect}, auto_evolve={args.auto_evolve}, "
          f"reload_skills={args.reload_skills}, push_enabled={not args.no_push}")

    agent = None
    try:
        # 1. PR 5: reload
        startup_hook(args, cfg)

        # 2.  AgentPR 1+2
        agent = create_team_context(
            goal=args.goal,
            agent_id=args.agent,
            compression_threshold=args.compression_threshold,
        )

        # 3.  PR 3  +  reload
        if args.backend:
            # PR 7: backend-driven
            backend_kwargs = json.loads(args.backend_config) if args.backend_config else {}
            run_agent_loop_with_backend(
                agent, args.backend, args.goal, args.iterations, cfg, backend_kwargs,
            )
        else:
            #  mock
            run_agent_loop(agent, args.goal, args.iterations, cfg)

        # 4. PR 4 evolve + PR 5 collect
        shutdown_hook(agent, args, cfg)

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
        if agent:
            agent.finalize()  #
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        if agent:
            try:
                agent.finalize()
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
