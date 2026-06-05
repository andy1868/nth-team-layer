"""A2A JSON-RPC 2.0 server skeleton (v0.10 T-8).

Exposes the two routes A2A consumers need to talk to us:

  GET  /.well-known/agent.json    serves a static Agent Card (T-7)
  POST /a2a/jsonrpc               JSON-RPC 2.0 endpoint

JSON-RPC method coverage (A2A v1.0):

  IMPLEMENTED in T-8:
    tasks/get
        Look up a Mission by id (we model A2A Tasks as Missions; see
        translate.a2a_task_from_mission) and render it as an A2A Task.
        TaskNotFoundError (-32001) when missing.

  STUBBED until v0.11:
    message/send                          (T-11)
    message/stream                        (T-11)
    tasks/cancel                          (T-11)
    tasks/subscribe                       (T-11)
    tasks/pushNotificationConfig/set      (T-12)
    tasks/pushNotificationConfig/get      (T-12)
    tasks/resubscribe                     (T-12)

  Stubbed methods return JSON-RPC error -32601 ("Method not found")
  with a structured ``data`` field naming the release in which the
  method is planned, so consumers can render an actionable error
  message rather than retrying blindly.

Conformance:
  * JSON-RPC 2.0 envelope (jsonrpc, method, params, id) per spec.
  * Standard error codes (-32700 parse, -32600 invalid request,
    -32601 method not found, -32602 invalid params, -32603 internal).
  * A2A-specific TaskNotFoundError = -32001.
  * Notifications (no ``id`` field) get no response body, per spec.
  * Batch requests handled: a JSON array of requests yields an array
    of responses (notifications stripped from the response array).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Tuple, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .agent_card import (
    A2A_WELL_KNOWN_PATH,
    validate_agent_card,
)
from .translate import a2a_task_from_mission

if TYPE_CHECKING:
    from ..orchestration import MissionStore

logger = logging.getLogger("nth_dao.a2a.server")


# ===== Error codes =====

# Standard JSON-RPC 2.0 error codes (https://www.jsonrpc.org/specification)
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# A2A-specific error codes (server-error space -32000..-32099)
A2A_TASK_NOT_FOUND = -32001


# ===== Method registry =====

# Methods we implement today.
A2A_METHODS_IMPLEMENTED: frozenset = frozenset({
    "tasks/get",
})

# Methods recognised by the A2A spec but not yet implemented in this
# build. Each maps to the planned release for the actionable error data.
A2A_METHODS_PLANNED: Dict[str, str] = {
    "message/send":                          "v0.11",
    "message/stream":                        "v0.11",
    "tasks/cancel":                          "v0.11",
    "tasks/subscribe":                       "v0.11",
    "tasks/pushNotificationConfig/set":      "v0.12",
    "tasks/pushNotificationConfig/get":      "v0.12",
    "tasks/resubscribe":                     "v0.12",
}


# ===== JsonRpcError =====


class JsonRpcError(Exception):
    """Raised inside a method handler to short-circuit with a structured
    JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_response(self, request_id: Any) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}


# ===== app factory =====


def create_a2a_app(
    *,
    agent_card: Dict[str, Any],
    mission_store: Optional["MissionStore"] = None,
    title: str = "NTH DAO A2A endpoint",
) -> FastAPI:
    """Build a FastAPI app exposing the A2A endpoints.

    Parameters
    ----------
    agent_card
        Pre-built and validated Agent Card dict (use
        ``nth_dao.a2a.build_agent_card``). Served verbatim at
        ``/.well-known/agent.json``. We validate again here so a caller
        that bypasses ``build_agent_card`` still gets caught.
    mission_store
        Optional MissionStore for resolving ``tasks/get`` requests.
        When None, ``tasks/get`` returns an internal error -32603 with
        a clear "no mission store configured" message so the consumer
        gets actionable feedback.
    title
        FastAPI app title (cosmetic; appears in /docs).
    """
    ok, reason = validate_agent_card(agent_card)
    if not ok:
        raise ValueError(f"refusing to serve invalid Agent Card: {reason}")

    # Defensive copy so callers can't mutate the served card after start.
    served_card = json.loads(json.dumps(agent_card))

    app = FastAPI(title=title)

    @app.get(A2A_WELL_KNOWN_PATH)
    def well_known_agent_card() -> Dict[str, Any]:
        return served_card

    @app.post("/a2a/jsonrpc")
    async def jsonrpc(request: Request) -> Response:
        # Parse the body as JSON; surface parse errors per JSON-RPC spec.
        try:
            raw = await request.body()
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(_error_response(None, JSONRPC_PARSE_ERROR, "Parse error"))

        # Batch (array) vs single request (object).
        if isinstance(payload, list):
            if not payload:
                return JSONResponse(_error_response(
                    None, JSONRPC_INVALID_REQUEST, "Empty batch",
                ))
            results = []
            for item in payload:
                resp = _dispatch(item, mission_store)
                if resp is not None:
                    results.append(resp)
            # If every item was a notification, return 204 per spec convention.
            if not results:
                return Response(status_code=204)
            return JSONResponse(results)

        resp = _dispatch(payload, mission_store)
        if resp is None:
            return Response(status_code=204)
        return JSONResponse(resp)

    return app


# ===== dispatch =====


def _dispatch(
    payload: Any,
    mission_store: Optional["MissionStore"],
) -> Optional[Dict[str, Any]]:
    """Validate the envelope, route to the method handler, marshal the
    response. Returns None for notifications (no ``id``) per spec."""
    if not isinstance(payload, dict):
        return _error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request")

    if payload.get("jsonrpc") != "2.0":
        return _error_response(
            payload.get("id"), JSONRPC_INVALID_REQUEST,
            "jsonrpc field must be '2.0'",
        )

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return _error_response(
            payload.get("id"), JSONRPC_INVALID_REQUEST,
            "method field must be a non-empty string",
        )

    # Notifications: no "id" field at all (per JSON-RPC 2.0 spec).
    # Note: an id of null / "" / 0 is STILL a request, not a notification.
    is_notification = "id" not in payload
    rpc_id = payload.get("id")
    params = payload.get("params", {})

    try:
        result = _invoke_method(method, params, mission_store)
    except JsonRpcError as exc:
        if is_notification:
            return None
        return exc.to_response(rpc_id)
    except Exception as exc:   # noqa: BLE001
        logger.exception("a2a method %r raised", method)
        if is_notification:
            return None
        return _error_response(
            rpc_id, JSONRPC_INTERNAL_ERROR,
            f"Internal error: {type(exc).__name__}",
        )

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _invoke_method(
    method: str,
    params: Any,
    mission_store: Optional["MissionStore"],
) -> Any:
    """Route to the implementation; stubbed and unknown methods raise."""
    if method == "tasks/get":
        return _do_tasks_get(params, mission_store)
    if method in A2A_METHODS_PLANNED:
        raise JsonRpcError(
            JSONRPC_METHOD_NOT_FOUND,
            f"Method '{method}' recognised but not implemented in this release",
            data={
                "method": method,
                "planned_for": A2A_METHODS_PLANNED[method],
                "implemented_methods": sorted(A2A_METHODS_IMPLEMENTED),
            },
        )
    raise JsonRpcError(
        JSONRPC_METHOD_NOT_FOUND,
        f"Method not found: {method!r}",
        data={
            "implemented_methods": sorted(A2A_METHODS_IMPLEMENTED),
            "planned_methods": sorted(A2A_METHODS_PLANNED),
        },
    )


# ===== tasks/get implementation =====


def _do_tasks_get(
    params: Any,
    mission_store: Optional["MissionStore"],
) -> Dict[str, Any]:
    """Look up a Mission by id and render as an A2A Task.

    Per A2A v1.0, ``tasks/get`` params shape::

        {"id": "<task_id>"}            (preferred)
        {"taskId": "<task_id>"}        (legacy alias, accepted)

    Returns the A2A Task object. Raises TaskNotFoundError (-32001) if
    no Mission matches.
    """
    if not isinstance(params, dict):
        raise JsonRpcError(
            JSONRPC_INVALID_PARAMS,
            "params must be a JSON object",
        )
    task_id = params.get("id") or params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        raise JsonRpcError(
            JSONRPC_INVALID_PARAMS,
            "params.id (string) is required",
        )
    if mission_store is None:
        raise JsonRpcError(
            JSONRPC_INTERNAL_ERROR,
            "no mission store configured on this A2A server; "
            "tasks/get cannot resolve task ids",
        )
    try:
        mission = mission_store.get(task_id)
    except Exception as exc:   # noqa: BLE001
        logger.warning("mission_store.get(%r) raised: %s", task_id, exc)
        raise JsonRpcError(
            JSONRPC_INTERNAL_ERROR,
            f"mission_store lookup failed: {type(exc).__name__}",
        ) from exc
    if mission is None:
        raise JsonRpcError(
            A2A_TASK_NOT_FOUND,
            "TaskNotFoundError",
            data={"taskId": task_id},
        )
    return a2a_task_from_mission(mission)


# ===== response builders =====


def _error_response(
    request_id: Any, code: int, message: str, data: Any = None,
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


__all__ = [
    "A2A_METHODS_IMPLEMENTED",
    "A2A_METHODS_PLANNED",
    "A2A_TASK_NOT_FOUND",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "JsonRpcError",
    "create_a2a_app",
]
