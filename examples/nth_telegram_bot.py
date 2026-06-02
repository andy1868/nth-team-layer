"""NTH DAO Telegram Bot — expose the NTH DAO runtime through Telegram.

Run:
    python nth_telegram_bot.py

Requires in ``~/.hermes/.env``:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_ALLOWED_USERS=<comma-separated Telegram user IDs>
    DEEPSEEK_API_KEY=...

Commands:
    /start              register the user + greet
    /team               list discoverable agents on the workspace
    /kanban             render Blackboard as a kanban summary
    /mission_new <title> | <step1> ; <step2>
                        create a Mission (title | semicolon-separated steps)
    /mission_list       list active missions
    /evolve             trigger EvoLoop.run_once()
    /ledger             tail the last ~10 ledger entries
    <free text>         routed to DeepSeek with the agent's ledger context

Notes:
- Workspace is the repo root; the bot attaches via ``nth_dao.attach()``
  as agent_id ``telegram-bot`` and writes to the standard NTH DAO paths.
- DeepSeek is reached via the openai SDK with a custom base_url.
- Module import is side-effect-free: env validation and LLM/runtime
  initialisation are *lazy*. `_validate_env()` is called from `main()`
  before the bot starts; `get_llm()` and `get_runtime()` are called by
  individual handlers on first use. This makes the module safe to
  `import` from tests / linters without env vars set.

Original lazy-init pattern contributed by @andy1868 in PR #7.
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

# 1.  ~/.hermes/.env
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

# 2. Read env into module-level constants; do NOT exit on missing values —
#    that breaks pytest collection and any tooling that just imports this
#    module. Validation is deferred to `_validate_env()` (called by main).
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED = {u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")


def _validate_env() -> None:
    """Raise RuntimeError if required env vars are missing.

    Called from `main()`; safe to skip during `import` so tests and
    static analysis don't need the secrets present.
    """
    missing = []
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Set them in ~/.hermes/.env (see this module's docstring)."
        )


# 3.  nth_dao
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nth_dao as nth
from nth_dao import render_kanban           # facade re-export
from nth_dao.orchestration import StepStatus

# 4. openai SDK for DeepSeek (lazy-imported below to keep import-time
#    side effects minimal; helpful for partial installs).
from openai import OpenAI

# 5. telegram
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

#
#
#

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("nth-bot")
#  telegram  verbose
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

#
# NTH DAO
#

REPO = Path(__file__).resolve().parent.parent

# Lazy runtime + LLM singletons. We do NOT touch network / file-system on
# import — handlers call get_runtime() / get_llm() on first use.
_TEAM = None
_LLM = None


def get_runtime():
    """Attach to NTH DAO on first call; return the cached session."""
    global _TEAM
    if _TEAM is None:
        _TEAM = nth.attach(
            agent_id="telegram-bot",
            backend=None,                          # routed to DeepSeek below
            capabilities=["chat", "telegram", "qa"],
            groups=["bots"],
            workspace=REPO,
            start_heartbeat=True,
        )
        logger.info("NTH DAO attached: %s on %s", _TEAM.agent_id, _TEAM.workspace)
    return _TEAM


def get_llm() -> OpenAI:
    """Return the shared DeepSeek client, initialising it lazily."""
    global _LLM
    if _LLM is None:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set; cannot reach DeepSeek. "
                "Add it to ~/.hermes/.env."
            )
        _LLM = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")
    return _LLM


# Legacy name kept for backwards compatibility with the rest of this file
# (TEAM was referenced unconditionally before the lazy refactor). Modules
# that import this script directly should prefer get_runtime() / get_llm().
class _RuntimeProxy:
    """Tiny proxy so `TEAM.foo` lazily forwards to the real runtime."""
    def __getattr__(self, name):
        return getattr(get_runtime(), name)


TEAM = _RuntimeProxy()

#
#
#

def authorized_only(handler):
    """ ALLOWED  user_id  + """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = str(update.effective_user.id) if update.effective_user else ""
        if ALLOWED and uid not in ALLOWED:
            logger.warning(f"DENY user_id={uid} (not in allowlist)")
            await update.message.reply_text(
                f" Unauthorized (user_id={uid}). Ask admin to add you to TELEGRAM_ALLOWED_USERS."
            )
            return
        #  +  monitor
        try:
            user_name = update.effective_user.first_name if update.effective_user else "?"
            msg = update.message.text or ""
            handler_name = handler.__name__.replace("cmd_", "/")
            logger.info(f"CMD {handler_name:20s} from {user_name:10s} args=`{msg[:80]}`")
        except Exception:
            pass
        try:
            return await handler(update, context)
        except Exception as e:
            logger.exception(f"Handler {handler.__name__} failed: {e}")
            try:
                await update.message.reply_text(
                    f" Internal error in {handler.__name__}: {type(e).__name__}: {str(e)[:200]}"
                )
            except Exception:
                pass
    return wrapper

#
#  Handlers
#

@authorized_only
async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        " NTH DAO Bot  \n\n"
        "\n"
        "/team   agent \n"
        "/kanban  Blackboard \n"
        "/mission_new <> | <1> ; <2> ; ...\n"
        "    : /mission_new ship payments | design api ; build ui ; e2e tests\n"
        "/mission_list   mission\n"
        "/evolve   EvoLoop.run_once()\n"
        "/ledger   ledger \n\n"
        "   DeepSeek  ledger"
    )

@authorized_only
async def cmd_team(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    online = TEAM.registry.list_alive()
    if not online:
        await update.message.reply_text("(no agents online)")
        return
    lines = ["  Agent:\n"]
    for r in online:
        caps = ",".join(r.capabilities[:3]) or "-"
        lines.append(f" `{r.agent_id}` [{r.status}]  caps=[{caps}]  on {r.hostname}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized_only
async def cmd_kanban(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    entries = TEAM.blackboard.list()
    if not entries:
        await update.message.reply_text(" Blackboard /mission_new ")
        return
    kanban_text = render_kanban(entries, width=22)
    # Telegram  4096 + Markdown code block
    if len(kanban_text) > 3800:
        kanban_text = kanban_text[:3800] + "\n... (truncated)"
    await update.message.reply_text(f"```\n{kanban_text}\n```", parse_mode="Markdown")

@authorized_only
async def cmd_mission_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args) if ctx.args else ""
    if "|" not in args:
        await update.message.reply_text(
            ": /mission_new <> | <1> ; <2> ; ...\n"
            ": /mission_new ship payments | design api ; build ui ; e2e tests"
        )
        return
    title, _, steps_part = args.partition("|")
    title = title.strip()
    steps_descs = [s.strip() for s in steps_part.split(";") if s.strip()]
    if not steps_descs:
        await update.message.reply_text(" 1  ; ")
        return
    steps = [
        {"id": f"step-{i+1}", "description": d, "depends_on": [f"step-{i}"] if i > 0 else []}
        for i, d in enumerate(steps_descs)
    ]
    mission = TEAM.start_mission(title=title, goal=title, steps=steps)
    short_id = mission.id[:8]
    step_examples = "\n".join([
        f"  /complete_step {short_id} {s['id']} "
        for s in steps[:2]
    ])
    await update.message.reply_text(
        f" <b>Mission created</b>\n\n"
        f"id:    <code>{mission.id}</code>\n"
        f"title: {mission.title}\n"
        f"steps: {len(steps)} ({', '.join(s['id'] for s in steps)})\n\n"
        f" <b></b> ID = <code>{short_id}</code>:\n"
        f"  /mission_take             ( claim)\n"
        f"  /mission_show {short_id}\n"
        f"{step_examples}",
        parse_mode="HTML",
    )

@authorized_only
async def cmd_mission_list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    missions = TEAM.mission_store.list_active()
    if not missions:
        await update.message.reply_text("(no active missions)")
        return
    lines = [" Active Missions:\n"]
    for m in missions[:10]:
        p = m.progress()
        lines.append(
            f" `{m.id}` {m.title}\n"
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
        await update.message.reply_text(" No error signatures crossed ROI threshold yet.")
        return
    lines = [f" Evolved {len(results)} signature(s):\n"]
    for r in results:
        sig = r.decision.error_sig
        gate = r.gate.action.value if r.gate else "no-gate"
        lines.append(f" `{sig}`  {gate}")
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
        await update.message.reply_text(" Ledger ")
        return
    lines = [" Recent ledger entries:\n"]
    for e in entries[-10:]:
        sig = e.get("error_sig") or "ok"
        lines.append(f" [{sig[:20]}] cost={e.get('token_cost',0)}t action={e.get('action_type','?')[:25]}")
    await update.message.reply_text("\n".join(lines))


#
# Mission PR 8 MissionRunner  Telegram
#

def _resolve_mission(mission_id_prefix: str):
    """ mission_id  12 """
    for m in TEAM.mission_store.list_active():
        if m.id.startswith(mission_id_prefix):
            return m
    return None


@authorized_only
async def cmd_mission_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ Mission  +  notes"""
    if not ctx.args:
        await update.message.reply_text(": /mission_show <mission_id >")
        return
    m = _resolve_mission(ctx.args[0])
    if not m:
        await update.message.reply_text(f" mission `{ctx.args[0]}*`", parse_mode="Markdown")
        return
    p = m.progress()
    lines = [
        f" *{m.title}*  `{m.id}`",
        f"status: {m.status}  |  {p['done']}/{p['total']} done ({p['percent']}%)",
        f"owner:  {m.owner}",
        f"scope:  {m.scope}",
        "",
        "*Steps:*",
    ]
    for s in m.steps:
        assignee = s.assignee or ""
        deps = f" deps={s.depends_on}" if s.depends_on else ""
        lines.append(f" `{s.id}` \\[{s.status}] {s.description[:60]}  by `{assignee}`{deps}")
        for note in s.notes[-2:]:
            lines.append(f"     {note[:100]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized_only
async def cmd_mission_take(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """ capability  step  claim"""
    found = TEAM.runner.find_work()
    if not found:
        await update.message.reply_text(
            " No actionable step matching capabilities=" + str(TEAM.capabilities)
        )
        return
    m, s = found
    TEAM.runner.claim(m.id, s.id)
    TEAM.registry.update_status(status="busy", current_mission=m.id)
    await update.message.reply_text(
        f" Claimed step:\n"
        f"mission: `{m.id}` {m.title}\n"
        f"step:    `{s.id}` {s.description}\n"
        f" /complete\\_step {m.id[:8]} {s.id}",
        parse_mode="Markdown",
    )


@authorized_only
async def cmd_complete_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ step: /complete_step <mid> <sid> [note...]"""
    if len(ctx.args) < 2:
        await update.message.reply_text(": /complete_step <mission_id> <step_id> []")
        return
    m = _resolve_mission(ctx.args[0])
    if not m:
        await update.message.reply_text(f" mission `{ctx.args[0]}*`")
        return
    step_id = ctx.args[1]
    note = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else "completed via telegram"
    outcome = TEAM.runner.complete(m.id, step_id, note=note)
    if not outcome.success:
        await update.message.reply_text(f" Failed: step `{step_id}` not found")
        return
    TEAM.registry.update_status(status="idle")
    #  Mission
    fresh = TEAM.mission_store.get(m.id)
    extra = "\n Mission " if fresh and fresh.is_finished() else ""
    await update.message.reply_text(
        f" Step `{step_id}` marked DONE in mission `{m.id[:8]}`\n"
        f"note: {note}{extra}",
        parse_mode="Markdown",
    )


@authorized_only
async def cmd_fail_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ step : /fail_step <mid> <sid> <reason...>"""
    if len(ctx.args) < 3:
        await update.message.reply_text(": /fail_step <mission_id> <step_id> <>")
        return
    m = _resolve_mission(ctx.args[0])
    if not m:
        await update.message.reply_text(f" mission `{ctx.args[0]}*`")
        return
    step_id = ctx.args[1]
    reason = " ".join(ctx.args[2:])
    outcome = TEAM.runner.fail(m.id, step_id, reason=reason)
    TEAM.registry.update_status(status="idle")
    await update.message.reply_text(
        f" Step `{step_id}` marked FAILED in mission `{m.id[:8]}`\n"
        f"reason: {reason}",
        parse_mode="Markdown",
    )


@authorized_only
async def cmd_handoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """: /handoff <mid> <sid> <to_agent_id> [note]"""
    if len(ctx.args) < 3:
        await update.message.reply_text(
            ": /handoff <mission_id> <step_id> <to_agent_id> []\n"
            "to_agent_id  /team "
        )
        return
    m = _resolve_mission(ctx.args[0])
    if not m:
        await update.message.reply_text(f" mission `{ctx.args[0]}*`")
        return
    step_id = ctx.args[1]
    to_agent = ctx.args[2]
    note = " ".join(ctx.args[3:]) if len(ctx.args) > 3 else ""
    outcome = TEAM.runner.handoff(m.id, step_id, to_agent_id=to_agent, note=note)
    if not outcome.success:
        await update.message.reply_text(f" Failed: step `{step_id}` not found")
        return
    await update.message.reply_text(
        f" Step `{step_id}` handed off:\n"
        f"  mission: `{m.id[:8]}`\n"
        f"  to:      `{to_agent}`\n"
        f"  note:    {outcome.note}",
        parse_mode="Markdown",
    )


#
# Skill / Soul / Discovery / Audit
#

@authorized_only
async def cmd_skill_list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """ skills/registry/ HTML mode   desc """
    from html import escape as h
    vp = TEAM.memory.providers.get("VectorProvider")
    skills = getattr(vp, "skill_index", []) if vp else []
    if not skills:
        await update.message.reply_text(" (no skills indexed)")
        return
    lines = [f" <b>{len(skills)}</b> skills in registry:\n"]
    for s in skills[:20]:
        name = h(s.get("name", "?"))
        desc = h((s.get("desc") or "")[:70])
        lines.append(f" <code>{name}</code>  {desc}")
    if len(skills) > 20:
        lines.append(f"\n<i> and {len(skills)-20} more</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@authorized_only
async def cmd_skill_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ skill """
    if not ctx.args:
        await update.message.reply_text(": /skill_show <skill_id>")
        return
    skill_id = ctx.args[0]
    skill_path = REPO / "skills" / "registry" / f"{skill_id}.md"
    if not skill_path.exists():
        #
        candidates = list((REPO/"skills"/"registry").glob(f"*{skill_id}*.md"))
        if not candidates:
            await update.message.reply_text(f" skill `{skill_id}`")
            return
        skill_path = candidates[0]
    content = skill_path.read_text(encoding="utf-8")[:3500]
    await update.message.reply_text(
        f" `{skill_path.name}`:\n```\n{content}\n```",
        parse_mode="Markdown",
    )


@authorized_only
async def cmd_soul(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """ TEAM-SOUL.md"""
    soul_path = REPO / "skills" / "TEAM-SOUL.md"
    if not soul_path.exists():
        await update.message.reply_text("(TEAM-SOUL.md not found)")
        return
    content = soul_path.read_text(encoding="utf-8")[:3500]
    await update.message.reply_text(f" *TEAM-SOUL:*\n```\n{content}\n```", parse_mode="Markdown")


@authorized_only
async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ capability : /find <capability>"""
    if not ctx.args:
        await update.message.reply_text(": /find <capability>  : /find python")
        return
    cap = ctx.args[0]
    matches = TEAM.finder.find(capability=cap, exclude_agent_ids=[TEAM.agent_id])
    if not matches:
        await update.message.reply_text(
            f" No teammate online with capability `{cap}`\n"
            f"(Just me  `{TEAM.agent_id}`)",
            parse_mode="Markdown",
        )
        return
    lines = [f" Found {len(matches)} agent(s) with capability `{cap}`:\n"]
    for r in matches:
        lines.append(f" `{r.agent_id}` [{r.status}] on {r.hostname}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized_only
async def cmd_audit(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """ evolution_audit.jsonl  5 """
    audit_path = REPO / "sidechain" / "evolution_audit.jsonl"
    if not audit_path.exists():
        await update.message.reply_text(" Evolution audit EvoLoop ")
        return
    lines_raw = audit_path.read_text(encoding="utf-8").strip().split("\n")
    entries = []
    for line in lines_raw[-5:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    if not entries:
        await update.message.reply_text(" (parse error)")
        return
    lines = [" *Evolution Audit ( 5 ):*\n"]
    for e in entries:
        action = e.get("action", "?")
        skill = e.get("skill_id", "?")
        risk = e.get("risk_level", "?")
        verify = "" if e.get("verify_passed") else ""
        lines.append(f" `{skill}`  {action} (risk={risk}, verify={verify})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized_only
async def cmd_pending(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """ patch (PENDING_REVIEW)"""
    pending_dir = REPO / "sidechain" / "pending_patches"
    if not pending_dir.exists():
        await update.message.reply_text(" No pending patches awaiting review")
        return
    patches = sorted(pending_dir.glob("*.json"))
    if not patches:
        await update.message.reply_text(" No pending patches awaiting review")
        return
    lines = [f" *{len(patches)} pending patch(es):*\n"]
    for p in patches[:10]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            patch = data.get("patch", {})
            lines.append(f" `{patch.get('skill_id', p.stem)}`")
            lines.append(f"    risk: {patch.get('risk_level', '?')}  trigger: `{patch.get('error_sig', '?')}`")
        except Exception:
            lines.append(f" `{p.name}` (parse error)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized_only
async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """HTML mode  < >  parser"""
    await update.message.reply_text(
        " <b>NTH DAO Bot  </b>\n\n"
        " <b>Discovery</b>\n"
        "/team   agent \n"
        "/find &lt;cap&gt;  \n\n"
        " <b>Blackboard</b>\n"
        "/kanban  TODO/DOING/DONE \n\n"
        " <b>Mission Orchestration</b>\n"
        "/mission_new &lt;title&gt; | &lt;step1&gt; ; &lt;step2&gt;  \n"
        "/mission_list  \n"
        "/mission_show &lt;id&gt;   + \n"
        "/mission_take   claim  step\n"
        "/complete_step &lt;mid&gt; &lt;sid&gt; [note]\n"
        "/fail_step &lt;mid&gt; &lt;sid&gt; &lt;reason&gt;\n"
        "/handoff &lt;mid&gt; &lt;sid&gt; &lt;to_agent&gt; [note]\n\n"
        " <b>Knowledge</b>\n"
        "/skill_list   skills/registry/\n"
        "/skill_show &lt;id&gt;   skill\n"
        "/soul  TEAM-SOUL.md\n\n"
        " <b>Evolution &amp; Audit</b>\n"
        "/evolve   EvoLoop\n"
        "/audit   5  evolution audit\n"
        "/pending   patch\n"
        "/ledger   10  ledger\n\n"
        " <b>Chat</b>\n"
        "  DeepSeek v4  +  ledger\n\n"
        " <i>:  mission_id  6-8 </i>",
        parse_mode="HTML",
    )


#
#   DeepSeek
#

SYSTEM_PROMPT_BASE = (
    "You are NTH DAO Bot, a Telegram assistant powered by DeepSeek. "
    "You are part of a multi-agent team using NTH DAO. "
    "Reply concisely (max 200 words unless asked for more)."
)

@authorized_only
async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    user_name = update.effective_user.first_name if update.effective_user else "user"
    logger.info(f"chat from {user_name}: {user_text[:60]}")

    #  NTH DAO runtime
    memory_block = TEAM.memory.build_memory_context_block()
    system_prompt = SYSTEM_PROMPT_BASE + "\n\n" + memory_block

    #  DeepSeek
    await update.message.chat.send_action("typing")
    error_sig = None
    content = ""
    tokens = 0
    try:
        resp = await asyncio.to_thread(
            get_llm().chat.completions.create,
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
        content = f" DeepSeek error: {type(e).__name__}: {str(e)[:200]}"
        logger.error(f"DeepSeek failed: {e}")

    #  ledger
    TEAM.memory.providers["LedgerProvider"].record(
        agent_id=TEAM.agent_id,
        action_type="telegram_chat",
        result=content[:200],
        error_sig=error_sig,
        token_cost=tokens,
    )

    #  Telegram
    if len(content) > 4000:
        content = content[:4000] + "\n...(truncated)"
    await update.message.reply_text(content)

#
#
#

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", ""),
        BotCommand("help", ""),
        # Discovery
        BotCommand("team", " agent "),
        BotCommand("find", ": /find <capability>"),
        # Blackboard
        BotCommand("kanban", "Blackboard "),
        # Mission
        BotCommand("mission_new", " Mission: <title> | <step1> ; <step2>"),
        BotCommand("mission_list", " Mission"),
        BotCommand("mission_show", "Mission : /mission_show <id>"),
        BotCommand("mission_take", " claim  step"),
        BotCommand("complete_step", " step: <mid> <sid> [note]"),
        BotCommand("fail_step", " step : <mid> <sid> <reason>"),
        BotCommand("handoff", ": <mid> <sid> <to_agent>"),
        # Knowledge
        BotCommand("skill_list", ""),
        BotCommand("skill_show", ": /skill_show <id>"),
        BotCommand("soul", " TEAM-SOUL.md"),
        # Evolution & Audit
        BotCommand("evolve", " EvoLoop"),
        BotCommand("audit", "evolution_audit  5 "),
        BotCommand("pending", " patch"),
        BotCommand("ledger", " 10  ledger"),
    ])
    logger.info("Bot commands registered with Telegram (19 cmds)")


def main():
    _validate_env()
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()

    #  7
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("team", cmd_team))
    app.add_handler(CommandHandler("kanban", cmd_kanban))
    app.add_handler(CommandHandler("mission_new", cmd_mission_new))
    app.add_handler(CommandHandler("mission_list", cmd_mission_list))
    app.add_handler(CommandHandler("evolve", cmd_evolve))
    app.add_handler(CommandHandler("ledger", cmd_ledger))
    # PR 8
    app.add_handler(CommandHandler("mission_show", cmd_mission_show))
    app.add_handler(CommandHandler("mission_take", cmd_mission_take))
    app.add_handler(CommandHandler("complete_step", cmd_complete_step))
    app.add_handler(CommandHandler("fail_step", cmd_fail_step))
    app.add_handler(CommandHandler("handoff", cmd_handoff))
    #  / Audit
    app.add_handler(CommandHandler("skill_list", cmd_skill_list))
    app.add_handler(CommandHandler("skill_show", cmd_skill_show))
    app.add_handler(CommandHandler("soul", cmd_soul))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info(f"Bot starting (polling)... allowed_users={ALLOWED or '(open)'}")
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        logger.info("Detaching NTH DAO...")
        TEAM.detach()


if __name__ == "__main__":
    main()
