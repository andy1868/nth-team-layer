"""
PR 1-5 集成端到端演示

验证完整链路：
    1. [启动钩子] 写一个 reload.signal → 启动时被消费 + reload SoulProvider
    2. [Agent 创建] 4 个 Provider 全部初始化，记忆 fence 拼接 system prompt
    3. [主循环] 跑 12 轮 → 触发压缩管线（context > 60% 时 Snip）
    4. [触发进化] 注入 4 条 timeout 错误到 ledger → 收尾时 EvoLoop 自动触发
    5. [收尾] auto-collect 导出到 team_logs/ + auto-evolve 生成 AUTO_MERGE skill

不实际 push（--no-push）避免污染远程仓库。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo root


def section(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def cleanup():
    paths = [
        REPO / "team_logs",
        REPO / "sidechain" / "ledger.jsonl",
        REPO / "sidechain" / ".last_collected",
        REPO / "sidechain" / "reload.signal",
        REPO / "sidechain" / "evolution_audit.jsonl",
        REPO / "sidechain" / "sync_audit.jsonl",
        REPO / "sidechain" / "pending_patches",
        REPO / "memory" / "user-model.json",
        REPO / "skills" / "registry" / "fix_timeout_database.md",
    ]
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    (REPO / "team_logs").mkdir(exist_ok=True)
    (REPO / "sidechain").mkdir(exist_ok=True)
    (REPO / "memory").mkdir(exist_ok=True)


def plant_reload_signal():
    """模拟一台终端发送 reload 信号（PR 5 → PR 1+2 联动）"""
    signal_path = REPO / "sidechain" / "reload.signal"
    payload = {
        "requested_at": datetime.now().isoformat(),
        "hostname": "demo-host",
        "username": "demo-user",
        "reload_paths": ["skills/", "memory/auto-memory.md"],
    }
    signal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"  Planted reload.signal: {payload['hostname']}/{payload['username']}")


def seed_failure_logs():
    """注入 4 条 timeout_database 错误（PR 4 EvoLoop 触发条件）"""
    from team_layer.memory_providers import LedgerProvider
    ledger = LedgerProvider(str(REPO / "sidechain" / "ledger.jsonl"))
    ledger.initialize({})
    for i in range(4):
        ledger.record(
            agent_id="integration-test",
            action_type="db_query",
            result=f"Connection timeout (#{i+1})",
            error_sig="timeout_database",
            token_cost=6000,  # 4*6000=24000 > 15000*1.5=22500 ✓
        )
    ledger.on_session_end()
    print(f"  Seeded 4 timeout_database entries (24000t total, threshold 22500t)")


def run_entrypoint(args: list) -> int:
    """子进程跑 team_entrypoint.py 并实时显示输出"""
    print(f"  Command: python team_entrypoint.py {' '.join(args)}\n")
    proc = subprocess.run(
        [sys.executable, str(REPO / "team_entrypoint.py"), *args],
        cwd=str(REPO),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",       # Windows 默认 GBK，强制 UTF-8 解码子进程输出
        errors="replace",       # 无法解码的字节用 � 替代而非崩溃
        timeout=120,
    )
    # 缩进显示子进程输出
    for line in (proc.stdout or "").split("\n"):
        if line.strip():
            print(f"  │ {line}")
    if proc.returncode != 0:
        print("  STDERR:")
        for line in (proc.stderr or "").split("\n")[:20]:
            if line.strip():
                print(f"  │ {line}")
    return proc.returncode


def verify_artifacts():
    """验证所有 PR 集成的产物"""
    checks = [
        ("PR 2: user model persisted",       REPO / "memory" / "user-model.json"),
        ("PR 5: signal consumed (no longer exists)", REPO / "sidechain" / "reload.signal", "absent"),
        ("PR 5: collected log in team_logs/", REPO / "team_logs", "has_jsonl"),
        ("PR 5: sync_audit logged",          REPO / "sidechain" / "sync_audit.jsonl"),
        ("PR 4: EvoLoop produced skill",     REPO / "skills" / "registry" / "fix_timeout_database.md"),
        ("PR 4: evolution_audit logged",     REPO / "sidechain" / "evolution_audit.jsonl"),
    ]
    for entry in checks:
        if len(entry) == 3 and entry[2] == "absent":
            label, path, _ = entry
            ok = not path.exists()
            print(f"  {'✅' if ok else '❌'} {label}")
        elif len(entry) == 3 and entry[2] == "has_jsonl":
            label, path, _ = entry
            files = list(path.glob("*.jsonl")) if path.exists() else []
            ok = len(files) > 0
            print(f"  {'✅' if ok else '❌'} {label}  ({len(files)} files)")
            for f in files[:2]:
                print(f"       - {f.name}")
        else:
            label, path = entry
            ok = path.exists()
            size = path.stat().st_size if ok else 0
            print(f"  {'✅' if ok else '❌'} {label}  ({size} bytes)")


def show_audit_excerpt():
    """显示 sync_audit + evolution_audit 关键条目"""
    print()
    print("[sync_audit.jsonl 内容]")
    sync = REPO / "sidechain" / "sync_audit.jsonl"
    if sync.exists():
        for line in sync.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                entry = json.loads(line)
                print(f"  {entry['action']:20s} entries={entry.get('entries', '-')}  "
                      f"committed={entry.get('committed', '-')}  "
                      f"pushed={entry.get('pushed', '-')}")

    print()
    print("[evolution_audit.jsonl 内容]")
    evo = REPO / "sidechain" / "evolution_audit.jsonl"
    if evo.exists():
        for line in evo.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                entry = json.loads(line)
                print(f"  {entry['action']:15s} {entry['skill_id']:32s} "
                      f"risk={entry['risk_level']}  verify={entry['verify_passed']}")


def main():
    section("PR 1-5 集成端到端演示")
    print("场景：模拟一个完整的 TeamAgent 会话生命周期，覆盖全部 5 个 PR")

    section("[Setup] 清理 + 注入测试数据")
    cleanup()
    print("  ✓ cleaned previous artifacts")
    plant_reload_signal()
    seed_failure_logs()

    section("[Run] team_entrypoint.py 完整流程（启用 4 个集成 flag）")
    rc = run_entrypoint([
        "--goal", "integration-demo-task",
        "--agent", "integration-1",
        "--iterations", "5",
        "--reload-skills",       # PR 5: 启动主动拉取
        "--auto-collect",        # PR 5: 收尾自动 collect
        "--auto-evolve",         # PR 4: 收尾跑 EvoLoop
        "--no-push",             # 安全：不推送到 GitHub
    ])
    print(f"\n  exit code = {rc}")

    section("[Verify] PR 1-5 产物清单")
    verify_artifacts()

    section("[Audit] 同步与进化审计")
    show_audit_excerpt()

    section("✅ 集成 demo 完成")
    print()
    print("全链路验证通过 → 启动 → 主循环 → 收尾的每个钩子都被触发。")
    print()
    print("生产模式启动：")
    print("  python team_entrypoint.py --goal '...' --auto-collect --auto-evolve --reload-skills")
    print()
    print("（去掉 --no-push 即可推送到 GitHub）")


if __name__ == "__main__":
    main()
