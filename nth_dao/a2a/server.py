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

import copy
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Tuple, Union

try:  # Keep the core package importable without the optional web extra.
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
except ModuleNotFoundError:  # pragma: no cover - covered by wheel smoke tests.
    FastAPI = None  # type: ignore[assignment]
    Request = Any  # type: ignore[misc, assignment]
    JSONResponse = None  # type: ignore[assignment]
    Response = None  # type: ignore[assignment]

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
A2A_UNAUTHENTICATED = -32002    # V-25: caller failed the auth callable
A2A_REQUEST_TOO_LARGE = -32003   # V-24: body or batch exceeded its cap


# ===== Default resource caps =====
#
# V-24, V-26: hard ceilings the public endpoint enforces. The
# `max_request_bytes` / `max_batch_size` parameters to create_a2a_app
# let a deployment widen or tighten them; the defaults are sized for
# a typical Mission-fetch workload (a few KB per JSON-RPC call, batches
# of at most a few dozen items).

DEFAULT_MAX_REQUEST_BYTES = 1 << 20    # 1 MiB
DEFAULT_MAX_BATCH_SIZE = 32


# ===== Auth callable contract =====
#
# V-25: the JSON-RPC endpoint is unauthenticated by default, which is
# only safe on a loopback bind. `auth_callable` lets a deployment plug
# in a real check (bearer token, mTLS, IP allow-list, etc.) without
# this module depending on any specific auth library.
#
# Contract: an awaitable returning None on success, or raising
# JsonRpcError to refuse the call. We pass the Request object so the
# callable can read headers, query params, client IP, etc.
AuthCallable = Callable[[Request], Awaitable[None]]


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
    auth_callable: Optional[AuthCallable] = None,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    allow_unauthenticated: bool = False,
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
    auth_callable
        Voss V-25 hook. An awaitable ``f(request) -> None`` that runs
        before every JSON-RPC dispatch. Raise ``JsonRpcError(
        A2A_UNAUTHENTICATED, ...)`` to refuse. The Agent Card already
        advertises the security scheme (`security` / `securitySchemes`
        fields); this callable is what actually enforces it. When
        ``None`` (the default), ``allow_unauthenticated=True`` MUST
        also be set explicitly - the deny-by-default posture protects
        deployments that accidentally bind to 0.0.0.0.
    max_request_bytes
        Voss V-24 cap. Bodies larger than this are rejected with HTTP
        413. Default 1 MiB.
    max_batch_size
        Voss V-26 cap. Batches with more than this many requests are
        rejected with -32003. Default 32.
    allow_unauthenticated
        Explicit opt-in for "no auth callable supplied". Forces the
        deployer to acknowledge the deny-by-default override; useful
        for local-only / loopback-bind deployments and tests.

    Raises
    ------
    ValueError
        If the agent card is invalid, or if no auth callable was
        supplied and ``allow_unauthenticated`` was not explicitly set.
    """
    if FastAPI is None or JSONResponse is None or Response is None:
        raise ImportError("create_a2a_app requires the optional web extra: install nth-dao[web]")

    ok, reason = validate_agent_card(agent_card)
    if not ok:
        raise ValueError(f"refusing to serve invalid Agent Card: {reason}")
    if auth_callable is None and not allow_unauthenticated:
        raise ValueError(
            "create_a2a_app requires either auth_callable=<...> or "
            "allow_unauthenticated=True. The endpoint exposes Mission "
            "data to anyone who can reach it; an explicit decision is "
            "required (Voss V-25)."
        )
    if auth_callable is None:
        logger.warning(
            "A2A endpoint started WITHOUT authentication "
            "(allow_unauthenticated=True). Bind to loopback only or "
            "front with a reverse proxy that enforces auth."
        )
    if max_request_bytes <= 0:
        raise ValueError("max_request_bytes must be positive")
    if max_batch_size <= 0:
        raise ValueError("max_batch_size must be positive")

    # V-39: prefer copy.deepcopy over json round-trip so non-JSON-
    # serialisable values surface immediately rather than at
    # serve-time. validate_agent_card has already accepted the dict,
    # so any remaining quirks are caller bugs.
    served_card = copy.deepcopy(agent_card)

    # Voss V-50: compute ETag once at app creation. Card is immutable
    # for the life of this app instance (callers must re-create it
    # to publish updates), so the ETag is stable. Consumers sending
    # If-None-Match get HTTP 304 with no body - saves a few KB per
    # poll on busy registries.
    served_card_bytes = json.dumps(
        served_card, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
    served_card_etag = '"' + hashlib.sha256(served_card_bytes).hexdigest()[:32] + '"'

    app = FastAPI(title=title)

    @app.get(A2A_WELL_KNOWN_PATH)
    def well_known_agent_card(request: Request) -> Response:
        # V-50: honour If-None-Match per RFC 7232 §3.2
        if request.headers.get("if-none-match") == served_card_etag:
            return Response(
                status_code=304,
                headers={"ETag": served_card_etag},
            )
        return JSONResponse(
            served_card,
            headers={
                "ETag": served_card_etag,
                # Card is "live until next deploy" - allow proxies to
                # cache briefly while still picking up new versions.
                "Cache-Control": "public, max-age=60, must-revalidate",
            },
        )

    @app.post("/a2a/jsonrpc")
    async def jsonrpc(request: Request) -> Response:
        # ----- V-25: authentication gate -----
        if auth_callable is not None:
            try:
                await auth_callable(request)
            except JsonRpcError as exc:
                return JSONResponse(
                    exc.to_response(None), status_code=401,
                )
            except Exception:    # noqa: BLE001
                logger.exception("auth_callable raised unexpectedly")
                return JSONResponse(
                    _error_response(None, JSONRPC_INTERNAL_ERROR,
                                    "auth callable failed"),
                    status_code=500,
                )

        # ----- V-24: body size cap -----
        try:
            declared_len = int(request.headers.get("content-length") or "0")
        except ValueError:
            declared_len = 0
        if declared_len > max_request_bytes:
            return JSONResponse(
                _error_response(
                    None, A2A_REQUEST_TOO_LARGE,
                    f"request body exceeds {max_request_bytes} bytes",
                ),
                status_code=413,
            )
        # Even when Content-Length is missing or lies, enforce by
        # accumulating chunks.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > max_request_bytes:
                return JSONResponse(
                    _error_response(
                        None, A2A_REQUEST_TOO_LARGE,
                        f"request body exceeds {max_request_bytes} bytes",
                    ),
                    status_code=413,
                )

        # Parse the body as JSON; surface parse errors per JSON-RPC spec.
        try:
            payload = json.loads(bytes(raw))
        except json.JSONDecodeError:
            return JSONResponse(_error_response(None, JSONRPC_PARSE_ERROR, "Parse error"))

        # Batch (array) vs single request (object).
        if isinstance(payload, list):
            if not payload:
                return JSONResponse(_error_response(
                    None, JSONRPC_INVALID_REQUEST, "Empty batch",
                ))
            # ----- V-26: batch size cap -----
            if len(payload) > max_batch_size:
                return JSONResponse(_error_response(
                    None, A2A_REQUEST_TOO_LARGE,
                    f"batch size {len(payload)} exceeds {max_batch_size}",
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
        # Voss V-40: don't leak the exception type to the caller.
        # An opaque correlation id goes back to the consumer; full
        # details land in the server log so an operator can correlate
        # without telling the network "you triggered a KeyError".
        ref_id = uuid.uuid4().hex[:12]
        logger.exception(
            "a2a method %r raised (ref=%s)", method, ref_id,
        )
        if is_notification:
            return None
        return _error_response(
            rpc_id, JSONRPC_INTERNAL_ERROR,
            f"Internal error (ref {ref_id})",
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

    Per A2A v1.0 / JSON-RPC 2.0, ``tasks/get`` params shape::

        {"id": "<task_id>"}            (by-name, preferred)
        {"taskId": "<task_id>"}        (legacy by-name alias)
        ["<task_id>"]                  (positional, Voss V-27 - JSON-RPC
                                        2.0 explicitly allows array params)

    Returns the A2A Task object. Raises TaskNotFoundError (-32001) if
    no Mission matches.
    """
    if isinstance(params, list):
        # Voss V-27: JSON-RPC 2.0 sec 4.2 permits by-position params.
        if len(params) != 1 or not isinstance(params[0], str) or not params[0]:
            raise JsonRpcError(
                JSONRPC_INVALID_PARAMS,
                "positional params for tasks/get must be ['<task_id>']",
            )
        task_id = params[0]
    elif isinstance(params, dict):
        # Voss V-41: prefer explicit-presence over truthy-`or` so an
        # empty `id` doesn't silently fall through to `taskId`.
        if "id" in params:
            task_id = params["id"]
        elif "taskId" in params:
            task_id = params["taskId"]
        else:
            raise JsonRpcError(
                JSONRPC_INVALID_PARAMS, "params.id (string) is required",
            )
        if not isinstance(task_id, str) or not task_id:
            raise JsonRpcError(
                JSONRPC_INVALID_PARAMS, "params.id must be a non-empty string",
            )
    else:
        raise JsonRpcError(
            JSONRPC_INVALID_PARAMS,
            "params must be a JSON object or array",
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
        ref_id = uuid.uuid4().hex[:12]
        logger.exception(
            "mission_store.get(%r) failed (ref=%s)", task_id, ref_id,
        )
        raise JsonRpcError(
            JSONRPC_INTERNAL_ERROR,
            f"Mission store lookup failed (ref {ref_id})",
            data={"ref": ref_id},
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
