"""
PR 5 多终端协同 — 端到端演示

场景模拟：
  - 两台终端（alice-laptop / bob-desktop）各自运行 TeamAgent
  - 各自记录 4 条 timeout_database 失败到本地 ledger
  - 各自调用 LogCollector 导出零冲突文件到 logs/（不实际 push，避免噪音）
  - 调用 CentralAggregator.run() 合并所有终端日志 → 触发 EvoLoop
  - 验证：
      ✓ logs/ 下生成 2 个零冲突命名的文件
      ✓ sidechain/aggregated_ledger.jsonl 包含 8 条（去重后）
      ✓ EvoLoop 触发 → fix_timeout_database AUTO_MERGE
      ✓ sidechain/aggregate_report.md 生成可读 PR 报告
      ✓ sync_audit.jsonl 记录所有操作
"""

import json
import shutil
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

from team_layer.git_sync import (
    SyncConfig,
    LogCollector,
    CentralAggregator,
    SkillLoader,
)
from team_layer.memory_providers import LedgerProvider


REPO_ROOT = Path(__file__).parent


def cleanup():
    """清理上次 demo 产物"""
    paths = [
        REPO_ROOT / "logs",
        REPO_ROOT / "sidechain" / "ledger.jsonl",
        REPO_ROOT / "sidechain" / "aggregated_ledger.jsonl",
        REPO_ROOT / "sidechain" / "aggregate_report.md",
        REPO_ROOT / "sidechain" / "evolution_audit.jsonl",
        REPO_ROOT / "sidechain" / "sync_audit.jsonl",
        REPO_ROOT / "sidechain" / ".last_collected",
        REPO_ROOT / "sidechain" / "pending_patches",
        REPO_ROOT / "skills" / "registry" / "fix_timeout_database.md",
    ]
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            print(f"  cleaned dir: {path.name}")
        elif path.exists():
            path.unlink()
            print(f"  cleaned file: {path.name}")


def simulate_terminal(hostname: str, username: str, agent_id: str, error_count: int):
    """模拟一台终端记录日志 + collect"""
    print(f"\n--- 终端: {hostname}/{username} (agent: {agent_id}) ---")

    # 用伪造的 hostname/username 构造 config
    cfg = SyncConfig(
        repo_root=REPO_ROOT,
        hostname=hostname,
        username=username,
        auto_push=False,  # demo 不真推
    )

    # 1. 写本地 ledger
    ledger = LedgerProvider(str(cfg.ledger_full_path()))
    ledger.initialize({})
    for i in range(error_count):
        ledger.record(
            agent_id=agent_id,
            action_type="db_query",
            result=f"Connection timeout from {hostname} (attempt {i+1})",
            error_sig="timeout_database",
            token_cost=6000,
        )
    ledger.on_session_end()
    print(f"  Wrote {error_count} ledger entries")

    # 2. Collect
    collector = LogCollector(cfg)
    result = collector.collect(auto_push=False)
    print(f"  {result}")

    # 重要：每个终端 collect 后清除本地 ledger（避免下一个终端读到旧数据）
    cfg.ledger_full_path().unlink(missing_ok=True)
    (cfg.sidechain_path() / ".last_collected").unlink(missing_ok=True)


def show_logs_dir():
    """展示 logs/ 目录内容（零冲突命名验证）"""
    logs_dir = REPO_ROOT / "logs"
    if not logs_dir.exists():
        print("  ❌ logs/ 不存在")
        return
    files = sorted(logs_dir.glob("*.jsonl"))
    print(f"\n📁 logs/ 中有 {len(files)} 个文件:")
    for f in files:
        size = f.stat().st_size
        lines = sum(1 for _ in f.open(encoding="utf-8")) if size > 0 else 0
        print(f"  - {f.name} ({size} bytes, {lines} entries)")


def show_aggregate_outputs():
    """展示汇总产物"""
    print()
    print("=" * 70)
    print("[Step 4] 验证汇总产物")
    print("=" * 70)

    artifacts = [
        ("Aggregated ledger", REPO_ROOT / "sidechain" / "aggregated_ledger.jsonl"),
        ("PR Report (Markdown)", REPO_ROOT / "sidechain" / "aggregate_report.md"),
        ("Auto-merged skill", REPO_ROOT / "skills" / "registry" / "fix_timeout_database.md"),
        ("Evolution audit", REPO_ROOT / "sidechain" / "evolution_audit.jsonl"),
        ("Sync audit", REPO_ROOT / "sidechain" / "sync_audit.jsonl"),
    ]
    for label, path in artifacts:
        if path.exists():
            print(f"  ✅ {label}: {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
        else:
            print(f"  ❌ {label}: MISSING")


def show_report_preview():
    """展示 PR 报告前 30 行"""
    report = REPO_ROOT / "sidechain" / "aggregate_report.md"
    if not report.exists():
        return
    print()
    print("=" * 70)
    print("[Step 5] PR 报告预览 (sidechain/aggregate_report.md)")
    print("=" * 70)
    content = report.read_text(encoding="utf-8")
    for line in content.split("\n")[:30]:
        print(f"  {line}")


def main():
    print("=" * 70)
    print("PR 5 多终端协同 — 端到端演示")
    print("=" * 70)

    print("\n[Step 0] 清理上一次演示产物...")
    cleanup()

    print("\n[Step 1] 模拟两台终端各自记录失败日志 + collect")
    simulate_terminal("alice-laptop", "alice", "worker-alice", error_count=4)
    simulate_terminal("bob-desktop", "bob", "worker-bob", error_count=4)

    print("\n[Step 2] 零冲突命名验证")
    show_logs_dir()

    print("\n[Step 3] 中央汇总器 — 合并 + 触发 EvoLoop")
    cfg = SyncConfig(repo_root=REPO_ROOT)
    aggregator = CentralAggregator(cfg, noise_min_count=2)
    report = aggregator.run(trigger_evolution=True)

    print(f"\n  Report summary:")
    print(f"    total_entries     = {report.total_entries}")
    print(f"    unique_hosts      = {report.unique_hosts}")
    print(f"    error_sigs        = {report.error_sigs}")
    print(f"    evolved_sigs      = {report.evolved_sigs}")
    print(f"    auto_merged       = {report.auto_merged}")
    print(f"    pending_review    = {report.pending_review}")

    show_aggregate_outputs()
    show_report_preview()

    print("\n[Step 6] SkillLoader 干跑测试（不实际发信号）")
    loader = SkillLoader(cfg)
    print(f"  Loader reload paths: {loader.reload_paths}")
    print(f"  Signal mechanism: {'pkill -HUP' if sys.platform != 'win32' else 'signal-file'}")
    print(f"  (Skipped actual reload to avoid touching working tree)")

    print()
    print("=" * 70)
    print("✅ PR 5 sync demo complete")
    print("=" * 70)
    print()
    print("In production:")
    print("  - LogCollector runs hourly on each terminal (cron/systemd)")
    print("  - CentralAggregator runs daily via GitHub Action")
    print("    → .github/workflows/team-evolve-daily.yml")
    print("  - SkillLoader runs after team merges Evolution PR")
    print("    → atomic checkout + reload signal")


if __name__ == "__main__":
    main()
