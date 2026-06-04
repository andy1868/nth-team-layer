"""Tests for nth_dao.agent_card — aggregated agent profile."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from nth_dao.agent_card import AgentCard


# ────────────────────────── Fixtures ──────────────────────────


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────── AgentCard dataclass ──────────────────────────


class TestAgentCard:
    def test_defaults(self):
        card = AgentCard(agent_id="alice")
        assert card.agent_id == "alice"
        assert card.label == ""
        assert card.health_score == 1.0
        assert card.is_alive

    def test_to_dict(self):
        card = AgentCard(
            agent_id="bob",
            label="Bob the Builder",
            capabilities=["python", "web"],
            health_score=0.85,
            reputation_score=4.2,
            reputation_count=10,
        )
        d = card.to_dict()
        assert d["agent_id"] == "bob"
        assert d["capabilities"] == ["python", "web"]
        assert d["health_score"] == 0.85

    def test_to_json(self):
        card = AgentCard(agent_id="alice")
        j = card.to_json()
        assert "alice" in j
        parsed = json.loads(j)
        assert parsed["agent_id"] == "alice"

    def test_short(self):
        card = AgentCard(
            agent_id="carol",
            label="Carol",
            capabilities=["deploy", "monitor"],
            health_score=0.9,
            reputation_score=4.5,
            reputation_count=5,
        )
        s = card.short()
        assert "Carol" in s
        assert "deploy" in s


# ────────────────────────── Rendering ──────────────────────────


class TestRender:
    def test_render_basic(self, tmp_workspace):
        card = AgentCard(
            agent_id="alice",
            label="Alice",
            capabilities=["python", "web"],
            backend_id="hermes",
            status="idle",
            health_score=1.0,
        )
        rendered = card.render()
        assert "Alice" in rendered
        assert "python" in rendered
        assert "idle" in rendered

    def test_render_offline(self):
        card = AgentCard(agent_id="bob", is_alive=False)
        rendered = card.render()
        assert "offline" in rendered

    def test_render_with_missions(self):
        card = AgentCard(
            agent_id="alice",
            missions_completed=5,
            missions_owned=10,
            success_rate=0.8,
        )
        rendered = card.render()
        assert "5 done" in rendered
        assert "80%" in rendered


# ────────────────────────── Build from sources ──────────────────────────


class TestBuild:
    def test_build_basic(self):
        card = AgentCard.build("test-agent")
        assert card.agent_id == "test-agent"
        assert card.health_score == 1.0

    def test_build_with_none_sources(self):
        card = AgentCard.build("test-agent", identity=None, registry=None)
        assert card.agent_id == "test-agent"


# ────────────────────────── Edge cases ──────────────────────────


class TestEdgeCases:
    def test_health_bar(self):
        from nth_dao.agent_card import _health_bar
        assert _health_bar(1.0) == "██████████"
        assert _health_bar(0.5) == "█████░░░░░"
        assert _health_bar(0.0) == "░░░░░░░░░░"
        assert _health_bar(0.33) == "███░░░░░░░"

    def test_trunc(self):
        from nth_dao.agent_card import _trunc
        assert _trunc("hello", 10) == "hello"
        assert _trunc("hello world this is long", 12) == "hello world…"

    def test_short_without_label(self):
        card = AgentCard(agent_id="no-label-123")
        s = card.short()
        assert "no-label-123" in s
