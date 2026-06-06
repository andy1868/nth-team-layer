"""F-9: external verifier gate for v0.10 mandate credentials."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from nth_dao.identity import AgentIdentity, crypto_available
from nth_dao.mandate.intent import build_intent_mandate, sign_intent_mandate
from nth_dao.conformance.external_vc import verify_with_didkit, verify_with_vcjs


def test_didkit_gate_reports_unavailable_without_internal_fallback(monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = verify_with_didkit({"id": "urn:test"}, command="didkit")
    assert result.available is False
    assert result.ok is False
    assert "not found" in result.reason


def test_vcjs_gate_requires_explicit_wrapper_command(monkeypatch):
    monkeypatch.delenv("NTH_DAO_VCJS_COMMAND", raising=False)
    result = verify_with_vcjs({"id": "urn:test"})
    assert result.available is False
    assert result.ok is False
    assert "not set" in result.reason


def test_vcjs_gate_handles_quoted_command_paths(tmp_path):
    script_dir = tmp_path / "dir with space"
    script_dir.mkdir()
    script = script_dir / "vcjs_ok.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    result = verify_with_vcjs(
        {"id": "urn:test"},
        command=f'"{sys.executable}" "{script}"',
    )
    assert result.available is True
    assert result.ok is True
    assert result.reason == "ok"


def test_vcjs_gate_accepts_argv_sequence_command(tmp_path):
    script = tmp_path / "vcjs_ok.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    result = verify_with_vcjs(
        {"id": "urn:test"},
        command=[sys.executable, str(script)],
    )
    assert result.available is True
    assert result.ok is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX script test")
def test_vcjs_gate_uses_external_process_return_code(tmp_path):
    script = tmp_path / "vcjs-ok"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    result = verify_with_vcjs({"id": "urn:test"}, command=str(script))
    assert result.available is True
    assert result.ok is True
    assert result.reason == "ok"


def test_vcjs_gate_uses_external_process_return_code_windows(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows-only command shape")
    script = tmp_path / "vcjs_fail.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    result = verify_with_vcjs(
        {"id": "urn:test"},
        command=[sys.executable, str(script)],
    )
    assert result.available is True
    assert result.ok is False
    assert "non-zero" in result.reason


def test_current_environment_has_no_external_vc_verifier_configured():
    """Documentation-by-test for the release gate.

    If this starts failing, the local/CI environment has a real verifier
    configured and the test suite should add a positive end-to-end vector.
    """
    if os.environ.get("NTH_DAO_VCJS_COMMAND"):
        pytest.skip("vc-js wrapper configured")
    if os.environ.get("NTH_DAO_DIDKIT_COMMAND"):
        pytest.skip("didkit command configured")
    assert verify_with_vcjs({"id": "urn:test"}).available is False


@pytest.mark.skipif(not crypto_available(), reason="PyNaCl required")
def test_release_gate_signed_intent_verifies_with_external_vc():
    """Release-mode F-9 gate for real cross-implementation evidence.

    Normal developer machines may not have DIDKit/vc-js installed, so the
    test skips by default. In release/CI, set NTH_DAO_REQUIRE_EXTERNAL_VC=1
    plus either NTH_DAO_VCJS_COMMAND or NTH_DAO_DIDKIT_COMMAND; then this test
    must pass against a real signed IntentMandate.
    """
    require_external = os.environ.get("NTH_DAO_REQUIRE_EXTERNAL_VC") == "1"
    dao = AgentIdentity.generate(label="f9-dao")
    agent = AgentIdentity.generate(label="f9-agent")
    intent = build_intent_mandate(
        issuer_did=dao.as_did(),
        agent_did=agent.as_did(),
        purpose="external verifier gate",
        constraints={
            "max_amount": {"value": "1.00", "currency": "USDC"},
            "allowed_counterparties": [],
            "allowed_settlement_methods": ["x402:usdc"],
        },
        expires_at=(
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
    )
    signed = sign_intent_mandate(intent, dao)

    if os.environ.get("NTH_DAO_VCJS_COMMAND"):
        result = verify_with_vcjs(signed)
    elif os.environ.get("NTH_DAO_DIDKIT_COMMAND") or shutil.which("didkit"):
        result = verify_with_didkit(signed)
    else:
        if require_external:
            pytest.fail(
                "NTH_DAO_REQUIRE_EXTERNAL_VC=1 but no external VC verifier "
                "is configured"
            )
        pytest.skip("no external VC verifier configured")

    assert result.available is True, result.reason
    assert result.ok is True, (
        f"{result.verifier} failed: {result.reason}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
