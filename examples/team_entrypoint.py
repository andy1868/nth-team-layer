"""
team_entrypoint.py — Nth Team Agent 统一启动入口（PR 1-5 集成）

集成链路：
    [启动钩子]
        ↓ 可选 --reload-skills → SkillLoader.reload() 拉最新技能
        ↓ 检查 sidechain/reload.signal（被动模式）
    [创建 TeamAgent]
        ↓ TeamMemoryManager(SoulProvider, UserModelProvider, VectorProvider, LedgerProvider)
        ↓ 拼接 system prompt with memory fence
    [主循环]
        ↓ 每轮：context usage 检查 → CompressionPipeline.auto_compress (5 层)
        ↓ 每轮：扫描 reload.signal（运行时热加载）
        ↓ 每轮：append_history + LedgerProvider.record
    [收尾钩子]
        ↓ agent.finalize() — 持久化 user model
        ↓ 可选 --auto-collect → LogCollector.collect → 推送到 team_logs/
        ↓ 可选 --auto-evolve → EvoLoop.run_once → AUTO_MERGE/PENDING_REVIEW

使用方式：
    # 基础（仅 PR 1-3）
    python team_entrypoint.py --goal "重构认证模块" --agent nlp-1

    # 启动前拉最新技能
    python team_entrypoint.py --goal "..." --reload-skills

    # 会话结束自动 collect + evolve（本地，不推送）
    python team_entrypoint.py --goal "..." --auto-collect --auto-evolve --no-push

    # 完整生产模式：自动收集 + 推送到 team_logs/
    python team_entrypoint.py --goal "..." --auto-collect --auto-evolve
"""

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

# Windows 兼容：强制 stdout/stderr UTF-8（避免 GBK 编码错误）
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


# ══════════════════════════════════════════════════════════════
# 钩子 1: 启动前
# ══════════════════════════════════════════════════════════════

def startup_hook(args, cfg: SyncConfig) -> None:
    """
    启动钩子：处理 reload 信号 + 可选主动拉取最新技能

    集成 PR 5 (SkillLoader) — 让 Agent 启动时使用最新团队技能
    """
    # 1. 被动模式：检查是否有待处理的 reload.signal
    pending = SkillLoader.check_reload_pending(cfg)
    if pending:
        print(f"[STARTUP] Pending reload signal detected from {pending.get('hostname')}")
        print(f"[STARTUP]   Paths to reload: {pending.get('reload_paths')}")
        # 信号已被消费（check 内部 unlink），下次循环用最新技能

    # 2. 主动模式：用户显式要求拉最新
    if args.reload_skills:
        print("[STARTUP] --reload-skills enabled, fetching latest skills...")
        try:
            loader = SkillLoader(cfg)
            result = loader.reload(send_signal=False)  # 自己就是 Agent，不用发信号
            print(f"[STARTUP]   {result}")
        except Exception as e:
            print(f"[STARTUP] reload failed (non-fatal): {e}")


# ══════════════════════════════════════════════════════════════
# 钩子 2: 创建 Agent
# ══════════════════════════════════════════════════════════════

def create_team_context(goal: str, agent_id: str, compression_threshold: float = 0.75) -> TeamAgent:
    """创建带完整记忆栈的 TeamAgent（集成 PR 1+2）"""
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


# ══════════════════════════════════════════════════════════════
# 钩子 3: 每轮迭代
# ══════════════════════════════════════════════════════════════

def per_iteration_hook(agent: TeamAgent, iteration: int, cfg: SyncConfig) -> None:
    """
    每轮钩子：压缩检查 + 运行时热加载

    集成 PR 3 (CompressionPipeline) + PR 5 (SkillLoader runtime reload)
    """
    # 1. 压缩检查（PR 3）
    if agent.should_compact():
        agent.trigger_compression()
        pipeline = CompressionPipeline(
            history=agent.history,
            effort_level="high",
        )
        msg = pipeline.auto_compress(threshold=agent.compression_threshold)
        print(f"[ITER {iteration}] {msg}")

    # 2. 运行时热加载（PR 5）— 每 5 轮检查一次（避免太频繁）
    if iteration > 0 and iteration % 5 == 0:
        pending = SkillLoader.check_reload_pending(cfg)
        if pending:
            print(f"[ITER {iteration}] Runtime reload signal from {pending.get('hostname')}")
            # 让 SoulProvider/VectorProvider 重新初始化（拿到最新技能）
            for name in ("SoulProvider", "VectorProvider"):
                provider = agent.team_mem.providers.get(name)
                if provider:
                    try:
                        provider.initialize({})
                        print(f"[ITER {iteration}]   reloaded {name}")
                    except Exception as e:
                        print(f"[ITER {iteration}]   {name} reload failed: {e}")


# ══════════════════════════════════════════════════════════════
# 钩子 4: 收尾
# ══════════════════════════════════════════════════════════════

def shutdown_hook(agent: TeamAgent, args, cfg: SyncConfig) -> None:
    """
    收尾钩子：持久化 + 可选 collect + 可选 evolve

    集成 PR 5 (LogCollector) + PR 4 (EvoLoop)
    """
    # 1. 持久化（PR 2）— 写 user model、刷 ledger
    agent.finalize()

    # 2. 自动 collect（PR 5）
    if args.auto_collect:
        print("\n[SHUTDOWN] --auto-collect: exporting session logs...")
        try:
            collector = LogCollector(cfg)
            result = collector.collect(auto_push=not args.no_push)
            print(f"[SHUTDOWN]   {result}")
        except Exception as e:
            print(f"[SHUTDOWN] collect failed (non-fatal): {e}")

    # 3. 自动 evolve（PR 4）
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


# ══════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════

def run_agent_loop_with_backend(
    agent: TeamAgent,
    backend_id: str,
    goal: str,
    max_iterations: int,
    cfg: SyncConfig,
    backend_kwargs: Optional[dict] = None,
) -> None:
    """
    PR 7: backend-driven 主循环

    使用任意 AgentBackend（mock / hermes / claude_code / openclaw / codex / openhands）
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

    print(f"\n✅ Backend session done — {result['session_summary'].total_turns} turns, "
          f"{result['session_summary'].total_usage.total} tokens")


def run_agent_loop(agent: TeamAgent, goal: str, max_iterations: int, cfg: SyncConfig) -> None:
    """
    主循环（mock 版本 — 实际应与 Hermes Agent.run() 集成）

    每轮：
        1. 触发 per_iteration_hook（压缩 + 运行时 reload）
        2. mock 一个动作（实际为 LLM 决策 + 工具执行）
        3. append_history + 记账
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

        # 每轮钩子（PR 3 + PR 5 运行时）
        per_iteration_hook(agent, iteration + 1, cfg)

        # mock 一个动作（真实场景：LLM 调用 + 工具执行）
        action = {"type": "think", "content": f"Working on: {goal}"}
        result = f"Progress: iteration {iteration + 1}"

        agent.append_history(action, result)

        # 记账（供 EvoLoop 后期溯源）
        agent.team_mem.providers["LedgerProvider"].record(
            agent_id=agent.agent_id,
            action_type="think",
            result=result,
            error_sig=None,
            token_cost=100,
        )

    print(f"\n✅ Completed {max_iterations} iterations")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Nth Team Agent — Hermes Team Layer 统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
集成 PR 1-5 完整链路：
  PR 1: TeamAgent 适配器
  PR 2: 4 记忆 Provider (Soul/User/Vector/Ledger)
  PR 3: 5 层压缩管线（每轮触发）
  PR 4: EvoLoop 自进化（--auto-evolve 启用）
  PR 5: 多终端协同（--auto-collect / --reload-skills 启用）

Examples:
  # 基础运行（仅本地）
  python team_entrypoint.py --goal "重构认证模块"

  # 启动前拉最新技能
  python team_entrypoint.py --goal "..." --reload-skills

  # 完整生产模式
  python team_entrypoint.py --goal "..." --auto-collect --auto-evolve --reload-skills
        """,
    )

    # 核心参数
    parser.add_argument("--goal", type=str, help="Agent 的目标任务 (使用 --list-backends 时可省略)")
    parser.add_argument("--agent", type=str, default="team-agent-1", help="Agent ID")
    parser.add_argument("--iterations", type=int, default=5, help="最大迭代次数")
    parser.add_argument("--compression-threshold", type=float, default=0.75,
                        help="压缩触发阈值 (0.0-1.0, 默认 0.75)")

    # PR 7: Backend 选择
    parser.add_argument("--backend", type=str, default=None,
                        choices=["mock", "hermes", "claude_code", "openclaw", "codex", "openhands"],
                        help="Agent backend (默认: 内置 mock 主循环)")
    parser.add_argument("--backend-config", type=str, default=None,
                        help="JSON 字符串，传递给 backend 构造器")
    parser.add_argument("--list-backends", action="store_true",
                        help="列出所有 backend 与可用性")

    # PR 5 集成 flag
    parser.add_argument("--reload-skills", action="store_true",
                        help="启动前主动拉最新技能 (调用 SkillLoader.reload)")
    parser.add_argument("--auto-collect", action="store_true",
                        help="会话结束自动 collect 日志到 team_logs/")
    parser.add_argument("--no-push", action="store_true",
                        help="禁用 git push（auto-collect 时只 commit 不 push）")

    # PR 4 集成 flag
    parser.add_argument("--auto-evolve", action="store_true",
                        help="会话结束自动跑 EvoLoop（本地进化）")

    args = parser.parse_args()

    # ─── PR 7: --list-backends 短路退出 ───
    if args.list_backends:
        desc = default_registry.describe(refresh=True)
        print("Registered backends:")
        for bid, info in desc.items():
            status = "✅ AVAILABLE  " if info["available"] else "⛔ unavailable"
            note = info["capabilities"].get("notes", "")
            print(f"  {bid:15s} {status}  {note}")
            if info.get("error"):
                print(f"    └─ error: {info['error']}")
        sys.exit(0)

    if not args.goal:
        parser.error("--goal is required (unless --list-backends)")

    # 同步配置（auto_push 与 --no-push 联动）
    cfg = SyncConfig(auto_push=not args.no_push)
    print(f"[CONFIG] {cfg.describe()}")
    print(f"[CONFIG] backend={args.backend or 'built-in mock loop'}, "
          f"auto_collect={args.auto_collect}, auto_evolve={args.auto_evolve}, "
          f"reload_skills={args.reload_skills}, push_enabled={not args.no_push}")

    agent = None
    try:
        # 1. 启动钩子（PR 5: reload）
        startup_hook(args, cfg)

        # 2. 创建 Agent（PR 1+2）
        agent = create_team_context(
            goal=args.goal,
            agent_id=args.agent,
            compression_threshold=args.compression_threshold,
        )

        # 3. 主循环（含 PR 3 压缩 + 运行时 reload）
        if args.backend:
            # PR 7: backend-driven 主循环
            backend_kwargs = json.loads(args.backend_config) if args.backend_config else {}
            run_agent_loop_with_backend(
                agent, args.backend, args.goal, args.iterations, cfg, backend_kwargs,
            )
        else:
            # 经典 mock 循环
            run_agent_loop(agent, args.goal, args.iterations, cfg)

        # 4. 收尾钩子（PR 4 evolve + PR 5 collect）
        shutdown_hook(agent, args, cfg)

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
        if agent:
            agent.finalize()  # 紧急持久化
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
