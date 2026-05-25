"""
PR 7 多 Backend 协作 — 端到端演示

场景：一个跨平台 Agent Team 在同一个 Team Layer 下协作

  alice-frontend   → MockBackend (低延迟, 模拟 Hermes)
  bob-backend      → MockBackend (中延迟, 模拟 Claude Code)
  carol-codegen    → MockBackend (含 fail_rate, 模拟 Codex)

演示要点：
  1. Backend Registry 可用性探测
  2. 每个 Agent 用不同 backend 但共享同一 Team Layer:
       - 同一个 TEAM-SOUL.md (灵魂)
       - 同一个 Blackboard (协作看板)
       - 同一个 Ledger (跨 backend 错误统计)
  3. 任务流：carol 在 Blackboard 发任务 → alice/bob 各自执行 → 写回结果
  4. 跨 backend 学习：carol 故意失败 N 次 → EvoLoop 触发 → fix skill 自动加入 registry
     → alice/bob 下次启动时 VectorProvider 自动加载（共享智慧）
"""

import shutil
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/ -> repo root

from team_layer import TeamAgent, TeamMemoryManager
from team_layer.backends import default_registry
from team_layer.blackboard import Blackboard, BlackboardProvider, render_kanban
from team_layer.evolution import EvoLoop
from team_layer.memory_providers import (
    LedgerProvider,
    SoulProvider,
    UserModelProvider,
    VectorProvider,
)

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root


def section(t):
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


def cleanup():
    paths = [
        REPO / "blackboard" / "shared.jsonl",
        REPO / "blackboard" / "group_dev.jsonl",
        REPO / "sidechain" / "ledger.jsonl",
        REPO / "sidechain" / "evolution_audit.jsonl",
        REPO / "sidechain" / "pending_patches",
        REPO / "memory" / "user-model.json",
    ]
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    # 清理上次 EvoLoop 留下的 fix_* skills
    reg = REPO / "skills" / "registry"
    if reg.exists():
        for f in reg.glob("fix_carol_mock_*.md"):
            f.unlink()


def make_agent(agent_id: str, groups: list = None) -> TeamAgent:
    """构造一个完整的 TeamAgent（含所有 Provider）"""
    providers = [
        SoulProvider("skills/TEAM-SOUL.md"),
        UserModelProvider(f"memory/user-model.json"),
        VectorProvider("skills/registry"),
        LedgerProvider("sidechain/ledger.jsonl"),  # 所有 Agent 共享同一 ledger
        BlackboardProvider(
            agent_id=agent_id,
            blackboard_root=str(REPO / "blackboard"),
            groups=groups or [],
        ),
    ]
    mem = TeamMemoryManager(providers, session_id=f"{agent_id}_session")
    mem.initialize({"agent_id": agent_id})
    return TeamAgent(agent_id=agent_id, team_memory_manager=mem, compression_threshold=0.75)


def main():
    section("PR 7 多 Backend 协作演示")
    print("场景：3 个 Agent 用不同 backend，但共享同一个 Team Layer")

    section("[Step 0] 清理 + 初始化")
    cleanup()
    bb = Blackboard(REPO / "blackboard")

    section("[Step 1] Backend 可用性探测")
    desc = default_registry.describe(refresh=True)
    avail = []
    unavail = []
    for bid, info in desc.items():
        if info["available"]:
            avail.append(bid)
            print(f"  ✅ {bid:15s} ready")
        else:
            unavail.append(bid)
            print(f"  ⛔ {bid:15s} unavailable")
    print(f"\n  ({len(avail)} available, {len(unavail)} unavailable in current env)")
    print("  → demo 全部用 MockBackend (不同配置模拟不同 backend)")

    section("[Step 2] PM (carol) 在 shared 发布任务")
    t1 = bb.post(
        topic="实现用户登录页",
        author="carol",
        scope="shared",
        content="React 前端 + Express 后端，OAuth2",
        metadata={"assignee": "alice"},
    )
    t2 = bb.post(
        topic="设计 OAuth callback handler",
        author="carol",
        scope="shared",
        metadata={"assignee": "bob"},
    )
    t3 = bb.post(
        topic="自动生成测试用例",
        author="carol",
        scope="shared",
        metadata={"assignee": "carol"},  # carol 自己执行（代码生成）
    )
    print(f"  ✓ Posted: {t1.id} → assignee=alice")
    print(f"  ✓ Posted: {t2.id} → assignee=bob")
    print(f"  ✓ Posted: {t3.id} → assignee=carol")

    section("[Step 3] alice 用 MockBackend (模拟 Hermes) 处理她的任务")
    alice = make_agent("alice", groups=["dev"])
    alice_backend = default_registry.create("mock", latency_ms=5)
    bb.update(t1.id, author="alice", status="doing")
    result = alice.run_with_backend(
        alice_backend,
        goal="implement login page",
        max_turns=2,
    )
    bb.update(t1.id, author="alice", status="done",
              content=f"completed in {result['session_summary'].total_turns} turns")

    section("[Step 4] bob 用另一个 MockBackend (模拟 Claude Code) 处理后端")
    bob = make_agent("bob", groups=["dev"])
    bob_backend = default_registry.create("mock", latency_ms=10)
    bb.update(t2.id, author="bob", status="doing")
    bob.run_with_backend(
        bob_backend,
        goal="design oauth callback handler",
        max_turns=2,
    )
    bb.update(t2.id, author="bob", status="done")

    section("[Step 5] carol 用 fail-prone MockBackend (模拟 Codex 偶发失败)")
    print("  模拟 carol 重试 4 次独立 session，每次都失败 → 累计 ROI 触发 EvoLoop")
    carol = make_agent("carol")
    carol_backend = default_registry.create("mock", latency_ms=2)
    bb.update(t3.id, author="carol", status="doing")

    # 跑 4 次独立 session，每次都用 'fail' 关键字制造失败
    for attempt in range(4):
        result = carol.run_with_backend(
            carol_backend,
            goal=f"please fail this codegen (attempt {attempt+1})",
            max_turns=1,
            per_turn_prompt=lambda i, a: "please fail this codegen task (large token waste)",
            error_sig_fn=lambda r: "carol_mock_error",
        )
        # 制造大 token cost 让 EvoLoop 触发（每次 6000t 模拟昂贵失败）
        carol.team_mem.providers["LedgerProvider"].record(
            agent_id="carol",
            action_type="backend:mock",
            result="failed codegen",
            error_sig="carol_mock_error",
            token_cost=6000,
        )
    # 显式刷盘（buffer → ledger.jsonl）
    carol.team_mem.providers["LedgerProvider"].on_session_end()

    bb.update(t3.id, author="carol", status="blocked", content="error rate too high, retry needed")

    section("[Step 6] 共享 Ledger 累计统计")
    ledger_path = REPO / "sidechain" / "ledger.jsonl"
    ledger = LedgerProvider(str(ledger_path))
    ledger.initialize({})
    if ledger_path.exists():
        entries = sum(1 for line in ledger_path.open(encoding="utf-8") if line.strip())
        print(f"  Total ledger entries:    {entries}")
    print(f"  carol_mock_error count:  {ledger.count_error_occurrences('carol_mock_error')}")
    print(f"  carol_mock_error wasted: {ledger.sum_token_cost_by_sig('carol_mock_error')} tokens")
    print(f"  EVOLUTION_BUDGET:        15000  (threshold = 22500t)")

    section("[Step 7] EvoLoop 跨 backend 学习")
    print("  Trigger: count >= 3 AND wasted > budget * 1.5")
    loop = EvoLoop(ledger=ledger)
    results = loop.run_once()
    for r in results:
        print(f"  → {r.summary()}")
    if not results:
        print("  → no signatures crossed threshold this round")

    section("[Step 8] Kanban 视图 — 所有 scope")
    all_entries = bb.list()
    print(render_kanban(all_entries, width=30))

    section("[Step 9] 验证共享智慧：alice 重新启动，会看到 carol 失败带来的新 skill")
    new_alice_vp = VectorProvider("skills/registry")
    new_alice_vp.initialize({})
    fix_skills = [s for s in new_alice_vp.skill_index if "carol" in s["name"].lower()]
    if fix_skills:
        print(f"  ✅ {len(fix_skills)} new fix skill(s) available to ALL future agents:")
        for s in fix_skills:
            print(f"    - {s['name']}: {s['desc'][:70]}")
    else:
        print("  (no auto-merged fix skill yet — EvoLoop may have routed to pending_review)")
        pending_dir = REPO / "sidechain" / "pending_patches"
        if pending_dir.exists():
            for p in pending_dir.glob("*.json"):
                print(f"    PENDING: {p.name}")

    section("[Step 10] 文件布局")
    for label, path in [
        ("Ledger (cross-backend)",  REPO / "sidechain" / "ledger.jsonl"),
        ("Blackboard (shared)",     REPO / "blackboard" / "shared.jsonl"),
        ("Evolution audit",         REPO / "sidechain" / "evolution_audit.jsonl"),
    ]:
        if path.exists():
            lines = sum(1 for _ in path.open(encoding="utf-8"))
            print(f"  ✓ {label:30s} {path.relative_to(REPO)}  ({lines} lines)")

    section("✅ PR 7 multi-backend demo 完成")
    print()
    print("关键证明：")
    print("  1. 6 个 backend 通过 Registry 统一管理")
    print("  2. 3 个 Agent 用不同 backend，共享 Soul / Blackboard / Ledger")
    print("  3. 一个 backend 的失败 → EvoLoop 学习 → 全 backend 共享修复")
    print()
    print("生产使用：")
    print("  python team_entrypoint.py --backend hermes --goal '...' --auto-evolve")
    print("  python team_entrypoint.py --backend claude_code --goal '...' --auto-evolve")
    print("  python team_entrypoint.py --backend openhands --goal '...' --auto-evolve")
    print("  → 各 backend 协同共享一切学习与协作状态")


if __name__ == "__main__":
    main()
