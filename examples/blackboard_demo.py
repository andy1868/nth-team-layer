"""
PR 6 Blackboard — 端到端演示

场景：3 个 Agent 协作完成一个迷你 sprint

  alice (frontend 组)  —— 私有 + group:frontend + shared
  bob   (backend 组)   —— 私有 + group:backend + shared
  carol (PM / shared)  —— 仅 shared

演示要点：
  1. 三层 scope 写入（shared/group/private）
  2. update 追加版本，保留历史
  3. list/get 自动返回最新版本
  4. 权限：alice 不能写到 private:bob
  5. Kanban 视图 (TODO / DOING / DONE / BLOCKED)
  6. BlackboardProvider 注入 system prompt
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

from team_layer.blackboard import (
    Blackboard,
    Scope,
    BlackboardProvider,
    render_kanban,
    render_table,
)

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root
BB_ROOT = REPO / "blackboard"


def section(title: str):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def cleanup():
    """清理之前的 demo 产物（保留 .gitignore / .gitkeep）"""
    if BB_ROOT.exists():
        for f in BB_ROOT.glob("*.jsonl"):
            f.unlink()


def main():
    section("PR 6 Blackboard — 多 Agent 共享数据空间演示")

    print("\n[Setup] 清理上一次产物...")
    cleanup()
    bb = Blackboard(BB_ROOT)
    print(f"  Blackboard root: {BB_ROOT}")

    # ──────────────────────────────────────────────────────
    section("[Step 1] 3 个 Agent 各自发布初始任务")
    # ──────────────────────────────────────────────────────

    # Carol (PM) 在 shared 发布全局任务
    t1 = bb.post(
        topic="Q2 ship: 新版本支付流程",
        author="carol",
        scope="shared",
        content="多个 sub-task，由各组 owner 拆分",
        metadata={"priority": "P0", "deadline": "2026-06-15"},
    )
    print(f"  carol → shared: {t1.id} '{t1.topic}'")

    t2 = bb.post(
        topic="收集用户反馈 N=50",
        author="carol",
        scope="shared",
        status="doing",
    )
    print(f"  carol → shared: {t2.id} '{t2.topic}' [doing]")

    # Alice (frontend) 在 group:frontend 发布前端任务
    t3 = bb.post(
        topic="Refactor checkout flow",
        author="alice",
        scope="group:frontend",
        status="doing",
        metadata={"branch": "feat/checkout-v2"},
    )
    print(f"  alice → group:frontend: {t3.id} '{t3.topic}' [doing]")

    t4 = bb.post(
        topic="Add Stripe payment elements",
        author="alice",
        scope="group:frontend",
    )
    print(f"  alice → group:frontend: {t4.id} '{t4.topic}'")

    # Bob (backend) 在 group:backend
    t5 = bb.post(
        topic="设计支付 webhook handler",
        author="bob",
        scope="group:backend",
        status="doing",
    )
    print(f"  bob → group:backend: {t5.id} '{t5.topic}' [doing]")

    # Alice 在 private 记录自己的临时思考
    t6 = bb.post(
        topic="思考：CheckoutContext 拆 useReducer 还是 zustand？",
        author="alice",
        scope="private:alice",
        content="倾向 zustand，团队已有依赖",
        status="todo",
    )
    print(f"  alice → private:alice: {t6.id} '{t6.topic}' (本地)")

    # ──────────────────────────────────────────────────────
    section("[Step 2] update 任务状态（append 新版本）")
    # ──────────────────────────────────────────────────────

    bb.update(t2.id, author="carol", status="done", content="N=53, 含 Pro 用户 17 人")
    print(f"  carol UPDATE {t2.id} → done")

    bb.update(t3.id, author="alice", status="blocked",
              content="等待 bob 的 webhook contract", metadata_patch={"blocker": t5.id})
    print(f"  alice UPDATE {t3.id} → blocked (waiting bob)")

    bb.update(t5.id, author="bob", status="done",
              content="webhook schema 已发到 #api-design")
    print(f"  bob UPDATE {t5.id} → done")

    bb.update(t3.id, author="alice", status="doing", content="unblocked，开始集成")
    print(f"  alice UPDATE {t3.id} → doing (now version 3)")

    # ──────────────────────────────────────────────────────
    section("[Step 3] 验证权限隔离")
    # ──────────────────────────────────────────────────────

    try:
        bb.post(
            topic="试图侵入 bob 的私有空间",
            author="alice",  # 但 scope 是 private:bob
            scope="private:bob",
        )
        print("  ❌ 权限漏洞！alice 不应该能写 private:bob")
    except PermissionError as e:
        print(f"  ✅ 权限拦截成功: {e}")

    # ──────────────────────────────────────────────────────
    section("[Step 4] 版本历史")
    # ──────────────────────────────────────────────────────

    history = bb.history(t3.id)
    print(f"  {t3.id} 'Refactor checkout flow' 有 {len(history)} 个版本：")
    for v in history:
        print(f"    v{v.version}  [{v.status:7s}]  by {v.author}  @ {v.updated_at[:19]}")
        if v.content:
            print(f"             {v.content[:60]}")

    # ──────────────────────────────────────────────────────
    section("[Step 5] Kanban 视图 (所有 scope 合并)")
    # ──────────────────────────────────────────────────────

    all_entries = bb.list()
    print(render_kanban(all_entries, width=28))

    # ──────────────────────────────────────────────────────
    section("[Step 6] Kanban 视图 (仅 shared 范围)")
    # ──────────────────────────────────────────────────────

    shared_only = bb.list(scope=Scope.shared())
    print(render_kanban(shared_only, width=32))

    # ──────────────────────────────────────────────────────
    section("[Step 7] 表格视图 (按 author=alice 过滤)")
    # ──────────────────────────────────────────────────────

    alices_tasks = bb.list(author="alice")
    print(render_table(alices_tasks))

    # ──────────────────────────────────────────────────────
    section("[Step 8] BlackboardProvider 注入示例")
    # ──────────────────────────────────────────────────────

    print("  模拟 alice 启动 Agent 时的 system prompt 注入：\n")
    provider = BlackboardProvider(
        agent_id="alice",
        blackboard_root=str(BB_ROOT),
        groups=["frontend"],  # alice 属于 frontend 组
    )
    provider.initialize({})
    block = provider.prefetch("alice-session-1")
    print(block)

    print()
    print("  模拟 bob 启动时（注意 group 不同）：\n")
    provider_bob = BlackboardProvider(
        agent_id="bob",
        blackboard_root=str(BB_ROOT),
        groups=["backend"],
    )
    provider_bob.initialize({})
    print(provider_bob.prefetch("bob-session-1"))

    # ──────────────────────────────────────────────────────
    section("[Step 9] 文件布局")
    # ──────────────────────────────────────────────────────

    for f in sorted(BB_ROOT.glob("*.jsonl")):
        lines = sum(1 for _ in f.open(encoding="utf-8"))
        in_git = "✅ Git tracked" if not f.name.startswith("private_") else "⛔ Local only"
        print(f"  {f.name:35s}  {lines:3d} entries  {in_git}")

    section("✅ Blackboard demo 完成")
    print()
    print("CLI 用法：")
    print("  python -m team_layer.blackboard list")
    print("  python -m team_layer.blackboard view --scope shared")
    print("  python -m team_layer.blackboard post 'fix bug' --author alice --scope shared")
    print("  python -m team_layer.blackboard update <id> --status done --author alice")


if __name__ == "__main__":
    main()
