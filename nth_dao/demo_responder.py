"""DemoAgentResponder — a tiny "echo agent" so the UI shows replies.

Why this exists
---------------
A first-time user does this:
    1. Opens the web console
    2. Creates a DAO
    3. Types a message in the chat
    4. Waits for *something* to reply
    5. Concludes "this is dead"

The chat is in fact working — the message reaches the GroupManager and
is stored — but nothing is *responding* on the other end. This module
plugs the simplest possible responder into the post-message path so the
demo loop closes: send a message, see an "agent" reply.

What it actually does
---------------------
For every incoming user message in a DAO:

1. Skip if the sender is itself a demo agent (no feedback loops).
2. Skip if the channel topic / DAO description does not opt in
   (controlled by `is_responder_dao(slug)`; defaults to True for any
   DAO whose name contains "demo" OR whose policy is "open" — easy to
   override per-deployment).
3. Generate a short reply via `compose_reply(message_body, context)`.
   The default implementation is a templated acknowledgement that quotes
   a snippet of the user message. Production deployments override this
   with a real LLM call.
4. Post the reply back through `GroupManager.post_message` as the demo
   agent's `agent_id`.

This is intentionally NOT an LLM call by default — pulling in any LLM
SDK at this layer would force a dependency every install has to satisfy.
The replacement hook is one function call away (`set_compose_reply(...)`)
so a deployment that wants Claude / GPT / DeepSeek can wire it in in
five lines.

Limits
------
- Synchronous and in-process. Long replies block the HTTP response.
  For real production, swap this for an EventBus consumer running in
  a worker thread (the EventBus interface is already there).
- One demo agent per DAO. Multiple responders would risk overlapping
  replies and confused UI.
- Not signed. The demo agent has no Ed25519 keypair; its messages
  carry no signature. For a production-shaped responder, mint an
  AgentIdentity and pass it through.

Original "agents must respond visibly in chat" requirement raised by
the project owner; this module is the minimal closing of that loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("nth_dao.demo_responder")


DEFAULT_AGENT_ID = "echo-agent"
DEFAULT_AGENT_LABEL = "EchoAgent"
DEFAULT_REPLY_TEMPLATE = (
    "👋 Hi {sender}, EchoAgent here. I received your message:\n"
    "  > {snippet}\n"
    "(This is the demo responder. Wire `set_compose_reply()` to plug in "
    "a real LLM.)"
)
MAX_SNIPPET_LEN = 160


@dataclass
class ResponderContext:
    """Everything the reply composer needs to know about the request."""

    dao_slug: str
    channel_id: str
    sender_id: str
    sender_code: str
    body: str


# Pluggable reply composer. Default is the template above.
_compose: Callable[[ResponderContext], str] = (
    lambda ctx: DEFAULT_REPLY_TEMPLATE.format(
        sender=(ctx.sender_code or ctx.sender_id or "friend"),
        snippet=(ctx.body or "").strip()[:MAX_SNIPPET_LEN] or "(empty)",
    )
)


def set_compose_reply(fn: Callable[[ResponderContext], str]) -> None:
    """Replace the default templated composer with a custom function.

    Typical production override::

        from nth_dao.demo_responder import set_compose_reply, ResponderContext
        from openai import OpenAI
        client = OpenAI()

        def with_llm(ctx: ResponderContext) -> str:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": ctx.body}],
            )
            return resp.choices[0].message.content or ""
        set_compose_reply(with_llm)
    """
    global _compose
    _compose = fn


def compose_reply(ctx: ResponderContext) -> str:
    """Generate a reply body for the given message context."""
    return _compose(ctx)


def is_responder_dao(slug: str, *, policy: str = "", description: str = "") -> bool:
    """Per-DAO opt-in. The defaults are deliberately permissive so the
    first-time demo just works; a deployment can shadow this with a
    `RESPONDER_DAOS` env-var lookup."""
    if not slug:
        return False
    lowered = f"{slug} {description}".lower()
    if "demo" in lowered or "test" in lowered:
        return True
    if policy == "open":
        return True
    return False


def maybe_reply(
    groups: Any,
    *,
    dao_slug: str,
    channel_id: str,
    sender_id: str,
    body: str,
    responder_id: str = DEFAULT_AGENT_ID,
    dao_policy: str = "",
    dao_description: str = "",
) -> Optional[dict]:
    """Post a reply if this DAO opts in and the sender isn't us.

    Returns the posted message dict on success; None when no reply was
    generated (skip conditions, opt-out, empty body, etc.).
    """
    if not body or not body.strip():
        return None
    if sender_id == responder_id:
        return None
    if not is_responder_dao(dao_slug, policy=dao_policy, description=dao_description):
        return None

    # Lazy import so test fixtures can monkeypatch agent_code without circulars.
    from .agent_code import code_for_agent_id

    ctx = ResponderContext(
        dao_slug=dao_slug,
        channel_id=channel_id,
        sender_id=sender_id,
        sender_code=code_for_agent_id(sender_id),
        body=body,
    )
    reply_body = compose_reply(ctx)
    if not reply_body or not reply_body.strip():
        return None
    try:
        message = groups.post_message(channel_id, sender_id=responder_id, body=reply_body)
        return message.to_dict() if hasattr(message, "to_dict") else dict(message)
    except Exception as exc:
        # Don't let a responder failure break the user's primary POST.
        logger.warning("demo responder post failed in %s: %s", dao_slug, exc)
        return None


__all__ = [
    "DEFAULT_AGENT_ID",
    "DEFAULT_AGENT_LABEL",
    "ResponderContext",
    "compose_reply",
    "is_responder_dao",
    "maybe_reply",
    "set_compose_reply",
]
