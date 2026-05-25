"""
EvoLoop 端到端演示

场景：
1. 注入 4 条 timeout_database 失败日志（每条 cost 6000t，总 24000t > 15000 * 1.5）
2. 注入 2 条 lint_violation（不满足 count>=3）
3. 注入 4 条 destructive_drop_table（高风险，应进入 PENDING_REVIEW）
4. 运行 EvoLoop.run_once()，验证三种分支：
   - timeout_database  → AUTO_MERGE (low risk)
   - lint_violation    → 不触发（count 不足）
   - destructive_drop  → PENDING_REVIEW (high risk)
"""

import sys
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/ -> repo root

from team_layer.memory_providers import LedgerProvider
from team_layer.evolution import EvoLoop, EvoTrigger


def seed_ledger(ledger: LedgerProvider):
    """注入测试样本（模拟过去的失败日志）"""
    # 1. timeout_database — 4 次 × 6000t = 24000t > 22500 (15000*1.5) ✓
    for i in range(4):
        ledger.record(
            agent_id="worker-1",
            action_type="db_query",
            result=f"Connection timeout after 30s (attempt {i+1})",
            error_sig="timeout_database",
            token_cost=6000,
        )

    # 2. lint_violation — 仅 2 次（不满足 count>=3）
    for i in range(2):
        ledger.record(
            agent_id="worker-2",
            action_type="lint_check",
            result=f"E501 line too long (file{i}.py)",
            error_sig="lint_violation",
            token_cost=200,
        )

    # 3. destructive_drop_table — 4 次 × 6000t（高风险路径）
    for i in range(4):
        ledger.record(
            agent_id="worker-3",
            action_type="db_admin",
            result=f"DROP TABLE attempted on prod.users (request {i+1})",
            error_sig="destructive_drop_table",
            token_cost=6000,
        )

    # 持久化到磁盘
    ledger.on_session_end()


def cleanup_artifacts():
    """清理上一次 demo 产生的文件"""
    targets = [
        Path("sidechain/ledger.jsonl"),
        Path("sidechain/evolution_audit.jsonl"),
        Path("skills/registry/fix_timeout_database.md"),
        Path("skills/registry/fix_destructive_drop_table.md"),
        Path("sidechain/pending_patches/fix_destructive_drop_table.patch.json"),
        Path("sidechain/pending_patches/fix_timeout_database.patch.json"),
    ]
    for path in targets:
        if path.exists():
            path.unlink()
            print(f"  cleaned: {path}")


def main():
    print("=" * 70)
    print("EvoLoop 端到端演示 — PR 4")
    print("=" * 70)

    print("\n[Step 0] 清理上一次产物...")
    cleanup_artifacts()

    print("\n[Step 1] 初始化 Ledger 并注入测试样本...")
    ledger = LedgerProvider("sidechain/ledger.jsonl")
    ledger.initialize({})
    seed_ledger(ledger)
    print(f"  Total entries written: 4 + 2 + 4 = 10")

    print("\n[Step 2] 测试 Trigger 单独决策...")
    trigger = EvoTrigger(ledger, evolution_budget=15000)
    print(f"  Budget = {trigger.evolution_budget}, Threshold = {int(trigger.waste_threshold)}t")
    for sig in ("timeout_database", "lint_violation", "destructive_drop_table"):
        d = trigger.check(sig)
        marker = "✅" if d.should_evolve else "❌"
        print(f"  {marker} {d}")

    print("\n[Step 3] 运行 EvoLoop.run_once()...")
    loop = EvoLoop(ledger=ledger, trigger=trigger)
    results = loop.run_once()

    print(f"\n[Step 4] 详细结果（{len(results)} 个周期）：\n")
    for i, result in enumerate(results, 1):
        print(f"--- Cycle {i} ---")
        print(result.summary())
        print()

    print("=" * 70)
    print("[Step 5] 验证文件系统产物")
    print("=" * 70)

    auto_merged = Path("skills/registry/fix_timeout_database.md")
    pending = Path("sidechain/pending_patches/fix_destructive_drop_table.patch.json")
    audit = Path("sidechain/evolution_audit.jsonl")

    print(f"\n[AUTO_MERGE] {auto_merged}")
    if auto_merged.exists():
        print(f"  ✅ Exists ({auto_merged.stat().st_size} bytes)")
        print(f"  Preview (first 5 lines):")
        for line in auto_merged.read_text(encoding="utf-8").split("\n")[:5]:
            print(f"    {line}")
    else:
        print("  ❌ NOT FOUND")

    print(f"\n[PENDING_REVIEW] {pending}")
    if pending.exists():
        print(f"  ✅ Exists ({pending.stat().st_size} bytes)")
    else:
        print("  ❌ NOT FOUND")

    print(f"\n[AUDIT LOG] {audit}")
    if audit.exists():
        lines = audit.read_text(encoding="utf-8").strip().split("\n")
        print(f"  ✅ {len(lines)} audit entries")
        for line in lines:
            import json
            entry = json.loads(line)
            print(f"    - {entry['action'].upper()}: {entry['skill_id']} ({entry['reason']})")
    else:
        print("  ❌ NOT FOUND")

    print("\n" + "=" * 70)
    print("✅ EvoLoop demo complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
