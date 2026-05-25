"""
Nth Team Layer Telegram Bot — 把 Nth Team Layer 暴露到 Telegram

启动：
    python nth_telegram_bot.py

需要 ~/.hermes/.env 里有:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_ALLOWED_USERS=<comma-separated user IDs>
    DEEPSEEK_API_KEY=...

命令：
    /start           欢迎 + 命令列表
    /team            列出在线 agent (Discovery)
    /kanban          Blackboard kanban 视图
    /mission_new <title> | <step1> ; <step2>
                     新建 mission (步骤用 | 和 ; 分隔)
    /mission_list    列出所有 active mission
    /evolve          触发 EvoLoop.run_once()
    /ledger          ledger 统计 (近 10 条)
    任意文本         调 DeepSeek 回答 + 记入 ledger

设计：
- 共用同一个 workspace ~/Desktop/hermes-team-agent 的 nth 子系统
- 自己 attach() 一个 'telegram-bot' agent，让你的 Mission/Blackboard 操作有人格化的发起者
- LLM 直接走 openai SDK + DeepSeek（不通过 hermes 中间层）
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# 1. 加载 ~/.hermes/.env
def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

_load_dotenv(Path.home() / ".hermes" / ".env")

# 2. 必需依赖检查
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED = {u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN missing in ~/.hermes/.env"); sys.exit(1)
if not DEEPSEEK_KEY:
    print("ERROR: DEEPSEEK_API_KEY missing in ~/.hermes/.env"); sys.exit(1)

# 3. 加载 nth_team_layer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # examples/ -> repo
import nth_team_layer as nth
from nth_team_layer import render_kanban           # facade re-export
from nth_team_layer.orchestration import StepStatus

# 4. openai SDK for DeepSeek
from openai import OpenAI

# 5. telegram
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nth-bot")
# 抑制 telegram 库的 verbose 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────────
# Nth Team Layer 启动
# ─────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parent.parent  # examples/ -> repo
TEAM = nth.attach(
    agent_id="telegram-bot",
    backend=None,                                  # 我们自己直接调 DeepSeek
    capabilities=["chat", "telegram", "qa"],
    groups=["bots"],
    workspace=REPO,
    start_heartbeat=True,                          # 持续心跳让其他 agent 知道我们在线
)
logger.info(f"Nth Team Layer attached: {TEAM.agent_id} on {TEAM.workspace}")

# DeepSeek client
LLM = OpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com/v1",
)

# ─────────────────────────────────────────────────────────────────
# 权限装饰器
# ─────────────────────────────────────────────────────────────────

def authorized_only(handler):
    """只允许 ALLOWED 列表里的 user_id 调用"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = str(update.effective_user.id) if update.effective_user else ""
        if ALLOWED and uid not in ALLOWED:
            logger.warning(f"DENY user_id={uid} (not in allowlist)")
            await update.message.reply_text(
                f"⛔ Unauthorized (user_id={uid}). Ask admin to add you to TELEGRAM_ALLOWED_USERS."
            )
            return
        return await handler(update, context)
    return wrapper

# ─────────────────────────────────────────────────────────────────
# 命令 Handlers
# ─────────────────────────────────────────────────────────────────

@authorized_only
async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Nth Team Layer Bot — 已就绪\n\n"
        "命令：\n"
        "/team — 在线 agent 列表\n"
        "/kanban — Blackboard 看板\n"
        "/mission_new <标题> | <步骤1> ; <步骤2> ; ...\n"
        "    例: /mission_new ship payments | design api ; build ui ; e2e tests\n"
        "/mission_list — 所有进行中 mission\n"
        "/evolve — 触发 EvoLoop.run_once()\n"
        "/ledger — 最近 ledger 条目\n\n"
        "直接发文本 → 我用 DeepSeek 回答你（同时记入 ledger）"
    )

@authorized_only
async def cmd_team(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    online = TEAM.registry.list_alive()
    if not online:
        await update.message.reply_text("(no agents online)")
        return
    lines = ["🟢 在线 Agent:\n"]
    for r in online:
        caps = ",".join(r.capabilities[:3]) or "-"
        lines.append(f"• `{r.agent_id}` [{r.status}]  caps=[{caps}]  on {r.hostname}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized_only
async def cmd_kanban(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    entries = TEAM.blackboard.list()
    if not entries:
        await update.message.reply_text("📋 Blackboard 空。/mission_new 来加些任务")
        return
    kanban_text = render_kanban(entries, width=22)
    # Telegram 消息长度限制 4096 + Markdown code block
    if len(kanban_text) > 3800:
        kanban_text = kanban_text[:3800] + "\n... (truncated)"
    await update.message.reply_text(f"```\n{kanban_text}\n```", parse_mode="Markdown")

@authorized_only
async def cmd_mission_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args) if ctx.args else ""
    if "|" not in args:
        await update.message.reply_text(
            "用法: /mission_new <标题> | <步骤1> ; <步骤2> ; ...\n"
            "例: /mission_new ship payments | design api ; build ui ; e2e tests"
        )
        return
    title, _, steps_part = args.partition("|")
    title = title.strip()
    steps_descs = [s.strip() for s in steps_part.split(";") if s.strip()]
    if not steps_descs:
        await update.message.reply_text("至少需要 1 个步骤（用 ; 分隔）")
        return
    steps = [
        {"id": f"step-{i+1}", "description": d, "depends_on": [f"step-{i}"] if i > 0 else []}
        for i, d in enumerate(steps_descs)
    ]
    mission = TEAM.start_mission(title=title, goal=title, steps=steps)
    await update.message.reply_text(
        f"✅ Mission created\n"
        f"id:    `{mission.id}`\n"
        f"title: {mission.title}\n"
        f"steps: {len(steps)}",
        parse_mode="Markdown",
    )

@authorized_only
async def cmd_mission_list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    missions = TEAM.mission_store.list_active()
    if not missions:
        await update.message.reply_text("(no active missions)")
        return
    lines = ["📦 Active Missions:\n"]
    for m in missions[:10]:
        p = m.progress()
        lines.append(
            f"• `{m.id}` {m.title}\n"
            f"  status: {m.status} | {p['done']}/{p['total']} done ({p['percent']}%)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized_only
async def cmd_evolve(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    from team_layer.evolution import EvoLoop
    ledger = TEAM.memory.providers["LedgerProvider"]
    loop = EvoLoop(ledger=ledger)
    results = loop.run_once()
    if not results:
        await update.message.reply_text("🧬 No error signatures crossed ROI threshold yet.")
        return
    lines = [f"🧬 Evolved {len(results)} signature(s):\n"]
    for r in results:
        sig = r.decision.error_sig
        gate = r.gate.action.value if r.gate else "no-gate"
        lines.append(f"• `{sig}` → {gate}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized_only
async def cmd_ledger(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    # flush in-memory buffer + read recent
    ledger = TEAM.memory.providers["LedgerProvider"]
    entries = []
    if hasattr(ledger, "buffer") and ledger.buffer:
        entries.extend(ledger.buffer[-10:])
    ledger_path = REPO / "sidechain" / "ledger.jsonl"
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").strip().split("\n")[-10:]:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    if not entries:
        await update.message.reply_text("📒 Ledger 空")
        return
    lines = ["📒 Recent ledger entries:\n"]
    for e in entries[-10:]:
        sig = e.get("error_sig") or "ok"
        lines.append(f"• [{sig[:20]}] cost={e.get('token_cost',0)}t action={e.get('action_type','?')[:25]}")
    await update.message.reply_text("\n".join(lines))

# ─────────────────────────────────────────────────────────────────
# 自由文本 → DeepSeek
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_BASE = (
    "You are Nth Team Bot, a Telegram assistant powered by DeepSeek. "
    "You are part of a multi-agent team using Nth Team Layer. "
    "Reply concisely (max 200 words unless asked for more)."
)

@authorized_only
async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    user_name = update.effective_user.first_name if update.effective_user else "user"
    logger.info(f"chat from {user_name}: {user_text[:60]}")

    # 注入 Team Layer 记忆上下文
    memory_block = TEAM.memory.build_memory_context_block()
    system_prompt = SYSTEM_PROMPT_BASE + "\n\n" + memory_block

    # 调 DeepSeek
    await update.message.chat.send_action("typing")
    error_sig = None
    content = ""
    tokens = 0
    try:
        resp = await asyncio.to_thread(
            LLM.chat.completions.create,
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=600,
        )
        content = resp.choices[0].message.content or "(empty)"
        tokens = resp.usage.total_tokens if resp.usage else 0
        logger.info(f"DeepSeek OK: {tokens}t")
    except Exception as e:
        error_sig = f"deepseek_{type(e).__name__}"
        content = f"⚠️ DeepSeek error: {type(e).__name__}: {str(e)[:200]}"
        logger.error(f"DeepSeek failed: {e}")

    # 记入 ledger
    TEAM.memory.providers["LedgerProvider"].record(
        agent_id=TEAM.agent_id,
        action_type="telegram_chat",
        result=content[:200],
        error_sig=error_sig,
        token_cost=tokens,
    )

    # 回 Telegram
    if len(content) > 4000:
        content = content[:4000] + "\n...(truncated)"
    await update.message.reply_text(content)

# ─────────────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────────────

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "欢迎 + 命令列表"),
        BotCommand("team", "在线 agent 列表"),
        BotCommand("kanban", "Blackboard 看板"),
        BotCommand("mission_new", "新建 Mission: <title> | <step1> ; <step2>"),
        BotCommand("mission_list", "进行中的 Mission"),
        BotCommand("evolve", "触发 EvoLoop"),
        BotCommand("ledger", "近 10 条 ledger"),
    ])
    logger.info("Bot commands registered with Telegram")


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("team", cmd_team))
    app.add_handler(CommandHandler("kanban", cmd_kanban))
    app.add_handler(CommandHandler("mission_new", cmd_mission_new))
    app.add_handler(CommandHandler("mission_list", cmd_mission_list))
    app.add_handler(CommandHandler("evolve", cmd_evolve))
    app.add_handler(CommandHandler("ledger", cmd_ledger))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info(f"Bot starting (polling)... allowed_users={ALLOWED or '(open)'}")
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        logger.info("Detaching Nth Team Layer...")
        TEAM.detach()


if __name__ == "__main__":
    main()
