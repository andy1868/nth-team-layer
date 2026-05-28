"""
PR 8 NTH DAO — 端到端演示

场景：3 个 Agent 通过 nth.attach() 加入团队，互相发现，接力完成一个 Mission

  alice-frontend   ← capabilities=["python", "frontend", "react"]
  bob-backend      ← capabilities=["python", "backend", "api"]
  carol-qa         ← capabilities=["python", "testing"]

Mission：上线支付 v2（3 个 step，有依赖关系）：
  step 1: design API           → 需要 backend
  step 2: implement frontend   → 需要 frontend，依赖 step 1
  step 3: e2e tests            → 需要 testing，依赖 step 1 + 2

演示要点：
  1. 三个 Agent attach() 各自注册 → 互相发现
  2. PeerFinder 按 capability 精准定位
  3. MissionRunner.find_work() 自动按 capability 匹配 + 依赖检查
  4. 接力：alice 完成 step 1 → bob 启动看到 step 2 可做 → claim → 完成
  5. carol 启动 → 看到 step 3 可做（前两步已 done）→ 完成
  6. Mission 自动状态转换：planning → active → completed
"""

import shutil
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/ -> repo root

import nth_dao as nth
from nth_dao.orchestration import StepStatus

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root


def section(t):
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


def cleanup():
    paths = [
        REPO / "team_agents",
        REPO / "missions",
        REPO / "blackboard" / "shared.jsonl",
        REPO / "blackboard" / "group_payments.jsonl",
        REPO / "sidechain" / "ledger.jsonl",
        REPO / "memory" / "user-model.json",
    ]
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def main():
    section("PR 8 NTH DAO — Discovery + Mission Orchestration")
    print("场景：3 个 Agent 通过 attach() 加入团队 → 互相发现 → 接力完成 Mission")

    section("[Setup] 清理上一次产物")
    cleanup()

    # ──────────────────────────────────────────────────────
    section("[Step 1] alice attach() — 注册 + 启动心跳")
    # ──────────────────────────────────────────────────────
    alice = nth.attach(
        agent_id="alice-frontend",
        backend="mock",
        capabilities=["python", "frontend", "react"],
        groups=["payments"],
        workspace=REPO,
        start_heartbeat=False,  # demo 不要心跳线程（避免 detach 后死锁）
    )
    print(f"  ✓ alice registered: {alice.agent_id}")
    print(f"    backend={alice.backend_id}, caps={alice.capabilities}, groups={alice.groups}")

    # ──────────────────────────────────────────────────────
    section("[Step 2] alice 启动一个 Mission")
    # ──────────────────────────────────────────────────────
    mission = alice.start_mission(
        title="上线支付 v2",
        goal="整体重构支付流程：API → 前端 → 测试",
        scope="shared",
        priority="high",
        steps=[
            {
                "id": "design-api",
                "description": "设计 webhook + REST API contract",
                "required_capabilities": ["backend", "api"],
            },
            {
                "id": "impl-frontend",
                "description": "实现 React Checkout 组件",
                "required_capabilities": ["frontend", "react"],
                "depends_on": ["design-api"],
            },
            {
                "id": "e2e-tests",
                "description": "Playwright E2E 测试覆盖整链路",
                "required_capabilities": ["testing"],
                "depends_on": ["design-api", "impl-frontend"],
            },
        ],
    )
    print(f"  ✓ Mission created: {mission.id} '{mission.title}'")
    print(f"    Steps: {[s.id for s in mission.steps]}")
    print()
    print(f"    {mission.short()}")

    # ──────────────────────────────────────────────────────
    section("[Step 3] bob attach() — 注册并查看 team")
    # ──────────────────────────────────────────────────────
    bob = nth.attach(
        agent_id="bob-backend",
        backend="mock",
        capabilities=["python", "backend", "api"],
        groups=["payments"],
        workspace=REPO,
        start_heartbeat=False,
    )
    print(f"  ✓ bob registered: {bob.agent_id}")
    print()
    print(f"  bob.discover() (含自己):")
    for r in bob.discover():
        print(f"    {r.short()}")

    # ──────────────────────────────────────────────────────
    section("[Step 4] bob 查找一个 backend 同事 → 发现 alice 不是匹配（alice 是 frontend）")
    # ──────────────────────────────────────────────────────
    teammate = bob.find_teammate(needed_capabilities=["backend"], )
    if teammate and teammate.record.agent_id != bob.agent_id:
        print(f"  ✓ matched: {teammate.record.agent_id} (score={teammate.score})")
    else:
        print(f"  ✓ no other 'backend' agent — bob is the only one (himself excluded)")

    # bob 看到自己可做的 step
    bob_work = bob.runner.find_work()
    if bob_work:
        m, s = bob_work
        print(f"  ✓ bob.find_work() → Mission '{m.title}' step '{s.id}': {s.description}")
        print(f"    required={s.required_capabilities}, bob has {bob.capabilities} ✓")

    # ──────────────────────────────────────────────────────
    section("[Step 5] bob claim + 执行 step 'design-api'")
    # ──────────────────────────────────────────────────────
    bob.runner.claim(mission.id, "design-api")
    print(f"  ✓ bob claimed 'design-api' → status=active")

    # 模拟 backend 执行
    response = bob.backend.send_turn(
        "Design payment webhook & REST API",
        system_prompt=bob.memory.build_memory_context_block(),
    ) if bob.backend else None
    if response:
        print(f"    [mock backend response] {response.content[:80]}")

    bob.runner.complete(
        mission.id, "design-api",
        output={"api_spec": "POST /webhooks/payment, GET /api/orders/:id"},
        note="API contract draft posted to blackboard",
    )
    print(f"  ✓ bob completed 'design-api'")

    # ──────────────────────────────────────────────────────
    section("[Step 6] alice 重新检查 → 看到 step 2 现在可做（依赖已满足）")
    # ──────────────────────────────────────────────────────
    alice_work = alice.runner.find_work()
    if alice_work:
        m, s = alice_work
        print(f"  ✓ alice.find_work() → '{s.id}': {s.description}")
        print(f"    required={s.required_capabilities}, alice has {alice.capabilities} ✓")

        alice.runner.claim(mission.id, s.id)
        print(f"  ✓ alice claimed '{s.id}'")
        alice.runner.complete(
            mission.id, s.id,
            output={"component": "CheckoutForm.tsx"},
            note="React component shipped to feat/checkout-v2",
        )
        print(f"  ✓ alice completed '{s.id}'")

    # ──────────────────────────────────────────────────────
    section("[Step 7] carol attach() → e2e-tests 现在可做")
    # ──────────────────────────────────────────────────────
    carol = nth.attach(
        agent_id="carol-qa",
        backend="mock",
        capabilities=["python", "testing"],
        groups=["payments"],
        workspace=REPO,
        start_heartbeat=False,
    )
    print(f"  ✓ carol registered: {carol.agent_id}")

    print(f"\n  整个团队（discover）:")
    for r in carol.discover():
        print(f"    {r.short()}")

    carol_work = carol.runner.find_work()
    if carol_work:
        m, s = carol_work
        print(f"\n  ✓ carol.find_work() → '{s.id}': {s.description}")
        carol.runner.claim(mission.id, s.id)
        carol.runner.complete(
            mission.id, s.id,
            output={"test_count": 12, "coverage": "94%"},
            note="all green on playwright",
        )
        print(f"  ✓ carol completed '{s.id}'")

    # ──────────────────────────────────────────────────────
    section("[Step 8] Mission 自动转 completed — 进度可视化")
    # ──────────────────────────────────────────────────────
    final = alice.mission_store.get(mission.id)
    p = final.progress()
    print(f"  Mission: {final.short()}")
    print(f"  Status:  {final.status}")
    print(f"  Done:    {p['done']}/{p['total']} ({p['percent']}%)")
    print()
    print(f"  接力链：")
    for s in final.steps:
        assignees = " → ".join(s.previous_assignees + [s.assignee or "?"])
        print(f"    {s.id:18s} [{s.status:9s}]  by  {assignees}")
        for note in s.notes[-2:]:
            print(f"      📝 {note}")

    # ──────────────────────────────────────────────────────
    section("[Step 9] 验证持久化 — missions/*.json 内容")
    # ──────────────────────────────────────────────────────
    mfile = REPO / "missions" / f"{mission.id}.json"
    if mfile.exists():
        size = mfile.stat().st_size
        print(f"  ✓ {mfile.relative_to(REPO)}  ({size} bytes, will be git-synced)")

    afiles = list((REPO / "team_agents").glob("*.json"))
    print(f"\n  team_agents/ 心跳文件 ({len(afiles)} 个)：")
    for f in sorted(afiles):
        print(f"    - {f.name}")

    # ──────────────────────────────────────────────────────
    section("[Step 10] 干净 detach")
    # ──────────────────────────────────────────────────────
    for s in (alice, bob, carol):
        s.detach()
        print(f"  ✓ {s.agent_id} detached")

    section("✅ PR 8 nth-dao demo 完成")
    print()
    print("生产用法：")
    print("  import nth_dao as nth")
    print("  team = nth.attach(agent_id='...', backend='hermes', capabilities=[...])")
    print("  # 你的 Agent 主循环")
    print("  team.detach()")
    print()
    print("跨终端协作：把整个 workspace/ 放进 Git 仓库 + 用 PR 5 git_sync")
    print("→ 团队成员 attach() 后立即看到所有共享状态")


if __name__ == "__main__":
    main()
