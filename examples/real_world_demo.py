"""
真实场景 demo — Hermes Team 完整启动 + LLM 解决真实问题

任务：让 DeepSeek (via Hermes backend) 审查我们自己写的代码
       (team_layer/backends/hermes.py) 并提 3 条改进建议

完整链路：
  attach()                      ← 一行启动 Hermes Team
    ↓
  team.start_mission(...)       ← 3-step Mission 持久化到 missions/
    ↓
  team.runner.find_work()       ← MissionRunner 找下一个 step
    ↓
  team.runner.claim(step)       ← 标记 active + 写心跳
    ↓
  team.agent.run_with_backend(  ← Backend-driven 主循环
      backend=team.backend,
      goal=...                  ← 真实 prompt 喂给 DeepSeek
  )
    ↓
  HermesBackend.send_turn       ← subprocess hermes chat
    ↓
  hermes 读 ~/.hermes/.env      ← 拿到 OPENROUTER_API_KEY
    ↓
  openai SDK → DeepSeek API     ← 真实网络调用
    ↓
  TurnResponse + Ledger 记账    ← 失败也记
    ↓
  team.runner.complete/fail()   ← Mission 进度推进
    ↓
  team.detach()                 ← 心跳停 + 持久化

观察点：
  - team_agents/alice-coder.json   心跳文件
  - missions/<id>.json             Mission 完整轨迹
  - sidechain/ledger.jsonl         每个 turn 的记账
  - sidechain/evolution_audit.jsonl  如果失败累计够多
"""

import json
import shutil
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nth_dao as nth
from nth_dao.orchestration import StepStatus

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root


def section(t):
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


def cleanup():
    """清理上一次 demo 产物（不删 ~/.hermes/）"""
    paths = [
        REPO / "team_agents",
        REPO / "missions",
        REPO / "memory" / "user-model.json",
        REPO / "sidechain" / "ledger.jsonl",
        REPO / "sidechain" / "evolution_audit.jsonl",
        REPO / "sidechain" / "sync_audit.jsonl",
        REPO / "blackboard" / "shared.jsonl",
        REPO / "blackboard" / "group_dev.jsonl",
        REPO / "blackboard" / "private_alice-coder.jsonl",
    ]
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def main():
    section("Hermes Team — 真实场景 demo")
    print("任务：让 LLM 审查 team_layer/backends/hermes.py，提 3 条改进建议")
    print("Backend: hermes + DeepSeek")
    print()

    cleanup()

    # ─────────────────────────────────────────────────────────
    section("[Step 1] attach() — 启动一个真实 Hermes Team agent")
    # ─────────────────────────────────────────────────────────
    team = nth.attach(
        agent_id="alice-coder",
        backend="hermes",
        backend_kwargs={
            "model": "deepseek-chat",
            # hermes_args 可加 ["--max-turns", "1"] 限制 tool iters
        },
        capabilities=["python", "code-review", "refactor"],
        groups=["dev"],
        workspace=REPO,
        start_heartbeat=False,  # demo 不要后台线程
    )
    print(f"  ✓ Agent attached: {team.agent_id}")
    print(f"    backend_id:   {team.backend_id}")
    print(f"    backend cls:  {type(team.backend).__name__ if team.backend else 'none'}")
    print(f"    capabilities: {team.capabilities}")
    print(f"    workspace:    {team.workspace}")
    print(f"    心跳文件:     team_agents/{team.agent_id}.json")

    # ─────────────────────────────────────────────────────────
    section("[Step 2] discover() — 看团队都谁在线")
    # ─────────────────────────────────────────────────────────
    online = team.discover()
    print(f"  当前在线 ({len(online)} 个):")
    for r in online:
        print(f"    {r.short()}")

    # ─────────────────────────────────────────────────────────
    section("[Step 3] start_mission() — 真实任务（多 step 接力）")
    # ─────────────────────────────────────────────────────────
    mission = team.start_mission(
        title="审查 hermes.py 子进程实现",
        goal="读 team_layer/backends/hermes.py，找问题，提 3 条改进建议",
        scope="shared",
        priority="normal",
        steps=[
            {
                "id": "read-file",
                "description": "读取 team_layer/backends/hermes.py 的关键段（send_turn）",
                "required_capabilities": ["python"],
            },
            {
                "id": "review",
                "description": "审查代码 + 给出 3 条具体改进建议（格式：'建议 N: ...'）",
                "required_capabilities": ["code-review"],
                "depends_on": ["read-file"],
            },
        ],
    )
    print(f"  ✓ Mission created: {mission.id}")
    print(f"    title:  {mission.title}")
    print(f"    steps:  {[s.id for s in mission.steps]}")

    # ─────────────────────────────────────────────────────────
    section("[Step 4] 读取代码（本地 step，不调 LLM）")
    # ─────────────────────────────────────────────────────────
    work = team.runner.find_work()
    if not work:
        print("  [WARN] no actionable step")
        return
    m, s = work
    print(f"  ✓ find_work() → step '{s.id}': {s.description}")
    team.runner.claim(mission.id, s.id)

    # 真读文件
    code_path = REPO / "team_layer" / "backends" / "hermes.py"
    code = code_path.read_text(encoding="utf-8")
    snippet = code[code.find("def send_turn"):][:2200]  # 关键段约 2.2KB
    print(f"  ✓ Read {code_path.name} ({len(code)} bytes total, snippet {len(snippet)} chars)")
    team.runner.complete(
        mission.id, s.id,
        output={"snippet_len": len(snippet)},
        note=f"loaded {code_path.relative_to(REPO)}",
    )

    # ─────────────────────────────────────────────────────────
    section("[Step 5] 调 LLM 审查代码（真实 DeepSeek 调用）")
    # ─────────────────────────────────────────────────────────
    work = team.runner.find_work()
    if not work:
        print("  [WARN] no review step")
        return
    m, review_step = work
    print(f"  ✓ find_work() → step '{review_step.id}'")
    team.runner.claim(mission.id, review_step.id)

    prompt = f"""You are a senior Python reviewer. Read this code from
team_layer/backends/hermes.py and give exactly 3 concrete improvement
suggestions, numbered "Suggestion 1: ...", "Suggestion 2: ...", "Suggestion 3: ...".

Focus on: robustness, error handling, Windows compatibility, security.
Be specific (line of code or pattern). Don't generic-praise.

```python
{snippet}
```"""

    print(f"  📤 Prompt length: {len(prompt)} chars")
    print(f"  📤 Calling DeepSeek via Hermes backend...")
    print()

    # 真实 turn — 通过 team.agent.run_with_backend
    result = team.agent.run_with_backend(
        backend=team.backend,
        goal=prompt,
        max_turns=1,
    )

    # ─────────────────────────────────────────────────────────
    section("[Step 6] LLM 响应分析")
    # ─────────────────────────────────────────────────────────
    summary = result["session_summary"]
    turn = result["turns"][0] if result["turns"] else None

    print(f"  Backend session summary:")
    print(f"    total_turns:    {summary.total_turns}")
    print(f"    total_tokens:   {summary.total_usage.total}")
    print(f"    duration:       {summary.duration_seconds:.2f}s")
    print(f"    final_status:   {summary.final_status}")

    if turn:
        print(f"\n  Turn 1:")
        print(f"    finish_reason: {turn['finish_reason']}")
        print(f"    tokens:        {turn['tokens']}")
        print(f"    latency:       {turn['latency']:.2f}s")
        if turn['error']:
            print(f"    error:         {turn['error'][:120]}")
        if turn['response_content']:
            print(f"\n  ┌── LLM response (first 800 chars) ──")
            for line in turn['response_content'][:800].split("\n"):
                print(f"  │ {line}")
            print(f"  └──")

    # 根据结果决定 step 命运
    if turn and turn['finish_reason'] == 'stop' and turn['response_content']:
        team.runner.complete(
            mission.id, review_step.id,
            output={"response_excerpt": turn['response_content'][:200]},
            note="LLM 返回成功",
        )
    else:
        team.runner.fail(
            mission.id, review_step.id,
            reason=turn['error'] if turn else "no response",
        )

    # ─────────────────────────────────────────────────────────
    section("[Step 7] Mission + Ledger + 全局状态")
    # ─────────────────────────────────────────────────────────
    final = team.mission_store.get(mission.id)
    p = final.progress()
    print(f"  Mission status: {final.status}  ({p['done']}/{p['total']} done, {p['failed']} failed)")
    print(f"\n  Step trail:")
    for s in final.steps:
        print(f"    [{s.status:9s}] {s.id:12s} assignee={s.assignee}")
        for n in s.notes[-2:]:
            print(f"      └ {n[:120]}")

    print()
    print(f"  Ledger entries (in-memory + buffer):")
    # 显式 flush buffer 到磁盘（LedgerProvider 默认 batch 写）
    ledger_provider = team.memory.providers["LedgerProvider"]
    # 直接读 buffer 而不等 on_session_end（避免破坏后续步骤）
    if hasattr(ledger_provider, 'buffer') and ledger_provider.buffer:
        for entry in ledger_provider.buffer:
            sig = entry.get('error_sig') or 'success'
            print(f"    [{sig:25s}] cost={entry.get('token_cost', 0):5d}t  "
                  f"action={entry.get('action_type', '?')[:30]}")
    else:
        # 已 flush 到磁盘 — 从文件读
        ledger_path = REPO / "sidechain" / "ledger.jsonl"
        if ledger_path.exists():
            for line in ledger_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                sig = entry.get('error_sig') or 'success'
                print(f"    [{sig:25s}] cost={entry.get('token_cost', 0):5d}t  "
                      f"action={entry.get('action_type', '?')[:30]}")
        else:
            print(f"    (still in buffer — will flush on detach)")

    # ─────────────────────────────────────────────────────────
    section("[Step 8] EvoLoop 评估 — 这次失败够不够触发？")
    # ─────────────────────────────────────────────────────────
    from team_layer.evolution import EvoLoop
    evo = EvoLoop(ledger=team.memory.providers["LedgerProvider"])
    results = evo.run_once()
    if results:
        print(f"  ✨ Triggered! {len(results)} evolution(s):")
        for r in results:
            print(f"    {r.summary()}")
    else:
        print(f"  → Not triggered yet (need count≥3 AND wasted>budget*1.5)")
        print(f"    本次单次失败 ≠ 触发条件。如果连续运行多次失败，EvoLoop 会自动学到模式")

    # ─────────────────────────────────────────────────────────
    section("[Step 9] detach() — 干净收尾")
    # ─────────────────────────────────────────────────────────
    team.detach()
    print(f"  ✓ {team.agent_id} detached")
    print(f"  ✓ heartbeat file 已标记 offline（保留供审计）")
    print(f"  ✓ memory/user-model.json 持久化")

    section("✅ Real-world demo complete")
    print()
    print("看到了什么：")
    print("  1. attach() 一行启动 hermes team 真实可用")
    print("  2. Discovery + Mission + 接力调度 全部真实运行")
    print("  3. Hermes backend 真的调了 DeepSeek API")
    print("  4. LedgerProvider 准确记录每个 turn 的 cost/error")
    print("  5. EvoLoop 按 ROI 门槛工作（单次失败不会误触发）")
    print()
    print("如果 key 是有效的:")
    print("  - turn[0]['finish_reason'] == 'stop'")
    print("  - turn[0]['response_content'] == LLM 真实的 3 条改进建议")
    print("  - Mission status == 'completed'")


if __name__ == "__main__":
    main()
