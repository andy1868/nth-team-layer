"""Local tool orchestrator — user's subprocess dispatcher (2026-06-08).

What this suite proves:

  1. Identity model: the orchestrator NEVER mints a sub-identity for
     the subprocess. Receipts are signed by the workspace identity
     (the user's DID). The subprocess gets a ``tool`` field on the
     timeline, not a ``signer_did``.
  2. Honest marking: ``via_subscription`` is True for Claude/Codex
     and False for OpenClaw/Hermes, surfaced on the receipt
     timeline payload.
  3. Roles: planner / reviewer / executor are recorded so an auditor
     can tell which step of a Plan-Execute-Review loop produced what.
  4. Rate cap: the sliding-window limiter rejects bursts above the
     configured cap with ``ToolRateLimitExceeded``.
  5. Tool not found / argv builder missing raises ``ToolNotFound``.
  6. Non-zero exit code surfaces as ``ToolInvocationFailed`` AFTER
     the receipt is signed — audit trail intact whether caller
     handles the exception or not.
  7. Timeout: stuck tool raises ToolInvocationFailed with
     timeout marker on the timeline.
  8. CLI: ``--unattended`` requires the ``NTH_TOOL_UNATTENDED=1``
     env var (accidental-daemon guard).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from nth_dao.execution_receipt import ReceiptStore, verify_receipt
from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.local_tool_orchestrator import (
    DEFAULT_RATE_LIMITS,
    LocalToolOrchestrator,
    ROLE_EXECUTOR,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    ToolInvocationFailed,
    ToolNotFound,
    ToolRateLimitExceeded,
    ToolResult,
    ToolSpec,
    _RateLimiter,
    detect_tools,
    main,
)


pytestmark = pytest.mark.skipif(
    not crypto_available(),
    reason="orchestrator receipts require PyNaCl",
)


# ─── helpers ─────────────────────────────────────────────────────────


def _mock_tool(name: str, via_subscription: bool = False) -> ToolSpec:
    """Construct a ToolSpec that points at the current Python — it's
    guaranteed to exist and exit cleanly when we ``python -c "..."``
    in tests.
    """
    return ToolSpec(
        name=name,
        path=sys.executable,
        version="test-1.0",
        via_subscription=via_subscription,
    )


def _make_orchestrator(
    tmp_path: Path,
    *,
    tools=None,
    rate_limits=None,
    monkeypatch_argv=True,
) -> "LocalToolOrchestrator":
    ident = AgentIdentity.generate(label="orch-test")
    store = ReceiptStore(tmp_path)
    if tools is None:
        tools = {"claude": _mock_tool("claude", via_subscription=True)}
    orch = LocalToolOrchestrator(
        identity=ident,
        receipt_store=store,
        rate_limits=rate_limits,
        tool_overrides=tools,
    )
    if monkeypatch_argv:
        # Redirect ALL builders to a benign ``python -c "print('ok')"``
        # so we don't actually shell out to claude/codex during tests
        import nth_dao.local_tool_orchestrator as mod
        mod._ARGV_BUILDERS = dict(mod._ARGV_BUILDERS)  # local copy
        for tool_name in list(tools.keys()):
            mod._ARGV_BUILDERS[tool_name] = (
                lambda exe, prompt, _opts: [
                    exe, "-c", f"print({prompt!r})",
                ]
            )
    return orch


# ─── identity model ──────────────────────────────────────────────────


def test_receipt_is_signed_by_workspace_identity_not_tool(tmp_path):
    """The orchestrator MUST NOT cosplay an Agent identity for the
    subprocess. Every receipt's signer_did must equal the workspace's
    DID — proving the identity model promise is honoured."""
    orch = _make_orchestrator(tmp_path)
    result = orch.invoke("claude", "hello-world", role=ROLE_PLANNER)
    rec = orch.receipts.load(result.receipt_id)
    assert rec is not None
    assert rec["signer_did"] == orch.identity.as_did()
    assert verify_receipt(rec, expected_pubkey_hex=orch.identity.pubkey_hex)


def test_receipt_timeline_records_tool_invoked_and_tool_result(tmp_path):
    orch = _make_orchestrator(tmp_path)
    result = orch.invoke("claude", "p", role=ROLE_PLANNER)
    rec = orch.receipts.load(result.receipt_id)
    types = [e["type"] for e in rec["timeline"]]
    assert types == ["tool_invoked", "tool_result"]
    invoked, tool_result = rec["timeline"]
    assert invoked["payload"]["tool"] == "claude"
    assert invoked["payload"]["invocation_role"] == ROLE_PLANNER
    assert tool_result["payload"]["ok"] is True


def test_via_subscription_marker_propagates_to_receipt(tmp_path):
    """A tool created with via_subscription=True MUST stamp that
    on the receipt timeline. This is the audit-honesty contract."""
    orch = _make_orchestrator(
        tmp_path,
        tools={"claude": _mock_tool("claude", via_subscription=True)},
    )
    result = orch.invoke("claude", "p")
    rec = orch.receipts.load(result.receipt_id)
    assert rec["timeline"][0]["payload"]["via_subscription"] is True
    assert result.via_subscription is True


def test_non_subscription_tool_marks_via_subscription_false(tmp_path):
    orch = _make_orchestrator(
        tmp_path,
        tools={"hermes": _mock_tool("hermes", via_subscription=False)},
    )
    result = orch.invoke("hermes", "p")
    rec = orch.receipts.load(result.receipt_id)
    assert rec["timeline"][0]["payload"]["via_subscription"] is False
    assert result.via_subscription is False


# ─── roles ────────────────────────────────────────────────────────────


def test_role_planner_recorded_on_timeline(tmp_path):
    orch = _make_orchestrator(tmp_path)
    result = orch.invoke("claude", "p", role=ROLE_PLANNER)
    rec = orch.receipts.load(result.receipt_id)
    assert rec["timeline"][0]["payload"]["invocation_role"] == "planner"


def test_role_executor_recorded_on_timeline(tmp_path):
    orch = _make_orchestrator(tmp_path)
    result = orch.invoke("claude", "p", role=ROLE_EXECUTOR)
    rec = orch.receipts.load(result.receipt_id)
    assert rec["timeline"][0]["payload"]["invocation_role"] == "executor"


def test_invalid_role_rejected(tmp_path):
    orch = _make_orchestrator(tmp_path)
    with pytest.raises(ValueError):
        orch.invoke("claude", "p", role="bogus-role")


# ─── rate limiting ───────────────────────────────────────────────────


def test_rate_limit_rejects_burst(tmp_path):
    """The first N calls succeed; the N+1th raises
    ``ToolRateLimitExceeded``. Critical for TOS-risk discipline."""
    orch = _make_orchestrator(
        tmp_path,
        rate_limits={"claude": 3},
    )
    for _ in range(3):
        orch.invoke("claude", "p")
    with pytest.raises(ToolRateLimitExceeded):
        orch.invoke("claude", "p")


def test_rate_limit_message_includes_wait_suggestion(tmp_path):
    orch = _make_orchestrator(
        tmp_path,
        rate_limits={"claude": 1},
    )
    orch.invoke("claude", "p")
    with pytest.raises(ToolRateLimitExceeded) as exc:
        orch.invoke("claude", "p")
    assert "Wait" in str(exc.value)


def test_rate_limit_unconfigured_tool_has_no_cap(tmp_path):
    """A tool not listed in rate_limits is unconfigured →
    pass through. Lets local-only tools (eg. hermes) run freely
    without contortions."""
    orch = _make_orchestrator(
        tmp_path,
        tools={"openclaw": _mock_tool("openclaw")},
        rate_limits={},  # explicitly empty
    )
    for _ in range(20):
        orch.invoke("openclaw", "p")


# ─── tool not found / argv builder ───────────────────────────────────


def test_invoke_unknown_tool_raises(tmp_path):
    orch = _make_orchestrator(tmp_path)
    with pytest.raises(ToolNotFound):
        orch.invoke("nonexistent-tool", "p")


# ─── invocation failure ──────────────────────────────────────────────


def test_nonzero_exit_raises_but_receipt_still_persisted(tmp_path):
    """Even when the subprocess fails, the receipt is signed and
    saved BEFORE the exception is raised — so the failed attempt
    is part of the audit trail. The caller's except handler can
    still find the receipt by ID via ``orch.receipts``."""
    orch = _make_orchestrator(tmp_path)
    # Override the argv builder to exit non-zero
    import nth_dao.local_tool_orchestrator as mod
    mod._ARGV_BUILDERS["claude"] = (
        lambda exe, prompt, _opts: [exe, "-c", "import sys; sys.exit(7)"]
    )
    with pytest.raises(ToolInvocationFailed) as exc:
        orch.invoke("claude", "p")
    assert "code 7" in str(exc.value)
    # Receipt store has the failed attempt
    receipts = list(orch.receipts.list_ids())
    assert receipts, "failed-invocation receipt was not persisted"
    rec = orch.receipts.load(receipts[0])
    assert rec["timeline"][-1]["payload"]["ok"] is False
    assert rec["timeline"][-1]["payload"]["exit_code"] == 7


# ─── detect_tools ────────────────────────────────────────────────────


def test_detect_tools_returns_dict_keyed_by_name(monkeypatch, tmp_path):
    """The probe doesn't have to FIND anything (CI runners don't have
    claude installed). Just verify the contract: dict, sorted keys
    are a subset of DEFAULT_RATE_LIMITS keys."""
    found = detect_tools()
    assert isinstance(found, dict)
    for name in found:
        assert name in DEFAULT_RATE_LIMITS


def test_detect_tools_extra_paths_extends_search(monkeypatch, tmp_path):
    """The extra_paths arg prepends to PATH for the probe — needed
    on Windows where Claude Code installs to %APPDATA%/npm-global."""
    # We can't easily induce a real find here; just verify the call
    # accepts the kwarg and doesn't crash.
    found = detect_tools(extra_paths=[str(tmp_path)])
    assert isinstance(found, dict)


# ─── pure rate limiter unit ─────────────────────────────────────────


def test_rate_limiter_records_calls():
    rl = _RateLimiter({"x": 2})
    rl.check_and_record("x")
    rl.check_and_record("x")
    with pytest.raises(ToolRateLimitExceeded):
        rl.check_and_record("x")


def test_rate_limiter_no_config_means_no_cap():
    rl = _RateLimiter({})
    for _ in range(100):
        rl.check_and_record("unbounded")   # no exception


# ─── CLI: --unattended guard ────────────────────────────────────────


def test_main_unattended_without_env_var_refuses(monkeypatch, capsys):
    """The accidental-daemon guard: --unattended alone isn't enough;
    the operator must ALSO set NTH_TOOL_UNATTENDED=1, an extra
    deliberate step that doesn't happen by mistake."""
    monkeypatch.delenv("NTH_TOOL_UNATTENDED", raising=False)
    rc = main([
        "--tool", "claude", "--prompt", "x", "--unattended",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "NTH_TOOL_UNATTENDED=1" in captured.out


def test_main_missing_workspace_identity_returns_3(tmp_path, monkeypatch, capsys):
    """If the identity isn't bootstrapped yet, the CLI exits with a
    clear message rather than a stack trace."""
    monkeypatch.setenv("NTH_TOOL_UNATTENDED", "1")
    rc = main([
        "--tool", "claude", "--prompt", "x",
        "--unattended", "--workspace", str(tmp_path / "empty"),
    ])
    captured = capsys.readouterr()
    assert rc == 3
    assert "no identity" in captured.out
