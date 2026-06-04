"""Tests for nth_dao.action_routing — agent-native action dispatch system."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from nth_dao.action_routing import (
    ActionRouter,
    ActionRequest,
    ActionResponse,
    ActionStatus,
    RouteStrategy,
    HandlerInfo,
)


# ────────────────────────── Fixtures ──────────────────────────


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)


@pytest.fixture
def router(tmp_workspace):
    return ActionRouter(agent_id="alice", workspace=tmp_workspace)


@pytest.fixture
def router_with_handlers(router):
    router.register(
        "ping",
        lambda req: {"pong": True, "from": req.from_agent},
        description="Responds with pong",
    )
    router.register(
        "echo",
        lambda req: req.params,
        description="Echoes params back",
        input_schema={"type": "object"},
    )
    router.register(
        "fail",
        lambda req: (_ for _ in ()).throw(ValueError("intentional")),
        description="Always fails",
    )
    return router


# ────────────────────────── HandlerInfo ──────────────────────────


class TestHandlerInfo:
    def test_defaults(self):
        info = HandlerInfo(action_type="deploy")
        assert info.action_type == "deploy"
        assert info.description == ""
        assert info.input_schema == {}
        assert info.timeout_seconds == 300
        assert info.max_concurrent == 5

    def test_full(self):
        schema = {"type": "object", "properties": {"env": {"type": "string"}}}
        info = HandlerInfo(
            action_type="deploy",
            description="Deploy to env",
            input_schema=schema,
            timeout_seconds=600,
            max_concurrent=1,
        )
        assert info.description == "Deploy to env"
        assert info.input_schema == schema
        assert info.timeout_seconds == 600
        assert info.max_concurrent == 1


# ────────────────────────── ActionRequest ──────────────────────────


class TestActionRequest:
    def test_round_trip(self):
        req = ActionRequest(
            request_id="req-001",
            action_type="ping",
            from_agent="alice",
            to_agent="bob",
            params={"key": "val"},
        )
        d = req.to_dict()
        req2 = ActionRequest.from_dict(d)
        assert req2.request_id == "req-001"
        assert req2.action_type == "ping"
        assert req2.from_agent == "alice"
        assert req2.to_agent == "bob"
        assert req2.params == {"key": "val"}
        assert req2.strategy == RouteStrategy.DIRECT.value

    def test_short_id(self):
        req = ActionRequest(request_id="abc123def456", action_type="", from_agent="", to_agent="")
        assert req.short_id == "abc123de"

    def test_defaults(self):
        req = ActionRequest(request_id="r1", action_type="a", from_agent="x", to_agent="y")
        assert req.params == {}
        assert req.strategy == RouteStrategy.DIRECT.value
        assert req.correlation_id == ""
        assert req.reply_to == ""
        assert req.sig == ""
        assert req.timestamp  # non-empty


# ────────────────────────── ActionResponse ──────────────────────────


class TestActionResponse:
    def test_round_trip(self):
        resp = ActionResponse(
            request_id="req-001",
            action_type="ping",
            from_agent="alice",
            to_agent="bob",
            status=ActionStatus.COMPLETED.value,
            result={"pong": True},
            elapsed_ms=12.5,
        )
        d = resp.to_dict()
        resp2 = ActionResponse.from_dict(d)
        assert resp2.status == "completed"
        assert resp2.result == {"pong": True}
        assert resp2.ok

    def test_not_ok(self):
        resp = ActionResponse(
            request_id="r1", action_type="a", from_agent="x", to_agent="y",
            status=ActionStatus.FAILED.value, error="boom",
        )
        assert not resp.ok


# ────────────────────────── ActionRouter — registry ──────────────────────────


class TestRouterRegistry:
    def test_register_and_has_handler(self, router):
        router.register("ping", lambda req: True)
        assert router.has_handler("ping")
        assert not router.has_handler("missing")

    def test_register_overwrite(self, router):
        router.register("ping", lambda req: "v1")
        router.register("ping", lambda req: "v2")
        info = router.handler_info("ping")
        assert info is not None

    def test_unregister(self, router):
        router.register("ping", lambda req: True)
        assert router.unregister("ping")
        assert not router.has_handler("ping")
        assert router.unregister("ping") is False  # already gone

    def test_capabilities(self, router_with_handlers):
        caps = router_with_handlers.capabilities
        assert caps == ["echo", "fail", "ping"]

    def test_list_handlers(self, router_with_handlers):
        handlers = router_with_handlers.list_handlers()
        assert len(handlers) == 3
        names = [h.action_type for h in handlers]
        assert names == ["echo", "fail", "ping"]

    def test_handler_info(self, router_with_handlers):
        info = router_with_handlers.handler_info("ping")
        assert info is not None
        assert info.description == "Responds with pong"
        assert router_with_handlers.handler_info("nope") is None


# ────────────────────────── ActionRouter — handle ──────────────────────────


class TestRouterHandle:
    def test_successful_handler(self, router_with_handlers):
        req = ActionRequest(
            request_id="r1", action_type="ping",
            from_agent="bob", to_agent="alice",
        )
        resp = router_with_handlers.handle(req)
        assert resp.status == ActionStatus.COMPLETED.value
        assert resp.result == {"pong": True, "from": "bob"}
        assert resp.ok

    def test_unknown_action(self, router):
        req = ActionRequest(
            request_id="r1", action_type="nonexistent",
            from_agent="bob", to_agent="alice",
        )
        resp = router.handle(req)
        assert resp.status == ActionStatus.FAILED.value
        assert "no handler" in resp.error
        assert not resp.ok

    def test_handler_raises(self, router_with_handlers):
        req = ActionRequest(
            request_id="r1", action_type="fail",
            from_agent="bob", to_agent="alice",
        )
        resp = router_with_handlers.handle(req)
        assert resp.status == ActionStatus.FAILED.value
        assert "ValueError: intentional" in resp.error

    def test_handler_echo_params(self, router_with_handlers):
        req = ActionRequest(
            request_id="r1", action_type="echo",
            from_agent="bob", to_agent="alice",
            params={"message": "hello", "count": 3},
        )
        resp = router_with_handlers.handle(req)
        assert resp.status == ActionStatus.COMPLETED.value
        assert resp.result == {"message": "hello", "count": 3}

    def test_elapsed_tracked(self, router_with_handlers):
        req = ActionRequest(
            request_id="r1", action_type="ping",
            from_agent="bob", to_agent="alice",
        )
        resp = router_with_handlers.handle(req)
        assert resp.elapsed_ms >= 0


# ────────────────────────── ActionRouter — history ──────────────────────────


class TestRouterHistory:
    def test_responses_sent(self, router_with_handlers):
        for i in range(3):
            router_with_handlers.handle(ActionRequest(
                request_id=f"r{i}", action_type="ping",
                from_agent="bob", to_agent="alice",
            ))
        responses = router_with_handlers.responses_sent(limit=10)
        assert len(responses) == 3
        for r in responses:
            assert r.status == ActionStatus.COMPLETED.value

    def test_requests_received(self, router_with_handlers):
        router_with_handlers.handle(ActionRequest(
            request_id="req-1", action_type="ping",
            from_agent="bob", to_agent="alice",
        ))
        received = router_with_handlers.requests_received()
        assert len(received) == 1
        assert received[0].request_id == "req-1"

    def test_limit(self, router_with_handlers):
        for i in range(10):
            router_with_handlers.handle(ActionRequest(
                request_id=f"r{i}", action_type="ping",
                from_agent="bob", to_agent="alice",
            ))
        assert len(router_with_handlers.responses_sent(limit=3)) == 3


# ────────────────────────── ActionRouter — parse_incoming ──────────────────────────


class TestParseIncoming:
    def test_valid_action_request(self, router):
        msg = {
            "content_type": "action/request",
            "request_id": "req-x",
            "action_type": "deploy",
            "from_agent": "carol",
            "to_agent": "alice",
            "params": {"env": "prod"},
        }
        req = router.parse_incoming(msg)
        assert req is not None
        assert req.action_type == "deploy"
        assert req.from_agent == "carol"

    def test_not_action_request(self, router):
        msg = {"content_type": "text", "content": "hello"}
        assert router.parse_incoming(msg) is None

    def test_malformed(self, router):
        assert router.parse_incoming({}) is None

    def test_handle_incoming(self, router_with_handlers):
        msg = {
            "content_type": "action/request",
            "request_id": "req-in",
            "action_type": "ping",
            "from_agent": "bob",
            "to_agent": "alice",
            "params": {},
        }
        resp = router_with_handlers.handle_incoming(msg)
        assert resp is not None
        assert resp.ok

    def test_handle_incoming_non_action(self, router):
        msg = {"content_type": "text", "content": "hello"}
        assert router.handle_incoming(msg) is None


# ────────────────────────── ActionRouter — route (via channel) ──────────────────────────



# ────────────────────────── RouteStrategy ──────────────────────────


class TestRouteStrategy:
    def test_values(self):
        assert RouteStrategy.DIRECT.value == "direct"
        assert RouteStrategy.BEST_MATCH.value == "best_match"
        assert RouteStrategy.BROADCAST.value == "broadcast"
        assert RouteStrategy.ROUND_ROBIN.value == "round_robin"


# ────────────────────────── ActionStatus ──────────────────────────


class TestActionStatus:
    def test_values(self):
        assert ActionStatus.REQUEST.value == "request"
        assert ActionStatus.ACCEPTED.value == "accepted"
        assert ActionStatus.RUNNING.value == "running"
        assert ActionStatus.COMPLETED.value == "completed"
        assert ActionStatus.FAILED.value == "failed"
        assert ActionStatus.REJECTED.value == "rejected"
        assert ActionStatus.TIMED_OUT.value == "timed_out"


# ────────────────────────── Edge cases ──────────────────────────


class TestEdgeCases:
    def test_unregister_nonexistent(self, router):
        assert not router.unregister("nope")

    def test_capabilities_empty(self, router):
        assert router.capabilities == []

    def test_list_handlers_empty(self, router):
        assert router.list_handlers() == []

    def test_history_empty(self, router):
        assert router.responses_sent() == []
        assert router.requests_received() == []
        assert router.requests_sent() == []

    def test_repr(self, router):
        r = repr(router)
        assert "alice" in r
        assert "handlers=0" in r

    def test_repr_with_handlers(self, router_with_handlers):
        r = repr(router_with_handlers)
        assert "handlers=3" in r
