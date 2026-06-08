"""A2A Protocol JSON-RPC handler — L1-2 (2026-06-08).

Implements the **minimum viable** A2A RPC surface so that NTH DAO
nodes can receive A2A tasks from any spec-conforming consumer
(LangChain / Cohere / Salesforce / PayPal / SAP A2A clients, etc).

═══════════════════════════════════════════════════════════════════
Wire format alignment
═══════════════════════════════════════════════════════════════════

The A2A protocol is defined by ``a2aproject/A2A`` ``specification/a2a.proto``.
Although the proto uses google.api.http for REST bindings, the
community SDKs (and a2aprotocol.ai's landing page) advertise a
JSON-RPC 2.0 envelope. We honour the JSON-RPC convention because
that's what real A2A clients send.

Methods implemented:
  * ``message/send`` — create a Task or append a message to an
    existing context. Returns a Task object.
  * ``tasks/get`` — fetch a Task by id. Returns a Task object.
  * ``tasks/cancel`` — mark a Task as canceled. Returns a Task object.

Data model (subset of a2a.proto):

  Task:
    id: string
    context_id: string
    status: TaskStatus { state: TaskState; timestamp: int }
    artifacts: [Artifact]
    history: [Message]
    metadata: object

  Message:
    message_id: string
    context_id: string
    task_id: string
    role: ROLE_USER | ROLE_AGENT
    parts: [Part]

  Part: { kind: "text" | "data" | "file"; ... }

═══════════════════════════════════════════════════════════════════
Integration with L1-1 receipts
═══════════════════════════════════════════════════════════════════

Every accepted ``message/send`` call also emits a signed execution
receipt (per motebit execution-ledger@1.0). The receipt's
``timeline`` records:

  * one ``goal_started`` entry when the Task is created
  * one ``nth.post_message`` entry per inbound message
  * one ``goal_completed`` entry when state transitions to completed

The receipt_id is stored on the Task's ``metadata.nth_receipt_id``.
This is how NTH provides 工作量证明 for A2A: a third party with the
Task can look up the receipt, verify the signature chain, and prove
the agent really did the work.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from nth_dao.execution_receipt import (
    ReceiptStore,
    TYPE_GOAL_COMPLETED,
    TYPE_GOAL_STARTED,
    TYPE_NTH_POST_MESSAGE,
    TYPE_STEP_STARTED,
    TimelineEntry,
    now_ms,
    sign_receipt,
)

if TYPE_CHECKING:
    from nth_dao.identity import AgentIdentity

logger = logging.getLogger("nth_dao.a2a_rpc")


# ─── A2A wire constants ──────────────────────────────────────────────


# TaskState enum values, verbatim from a2a.proto
TASK_STATE_UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

TERMINAL_STATES = frozenset({
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELED,
    TASK_STATE_REJECTED,
})

ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

# JSON-RPC 2.0 error codes (RFC + A2A extensions)
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# A2A application errors (custom range)
A2A_TASK_NOT_FOUND = -32001
A2A_TASK_TERMINAL = -32002


# ─── RPC envelope helpers ────────────────────────────────────────────


def _rpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(
    req_id: Any, code: int, message: str, data: Any = None,
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ─── Task store ──────────────────────────────────────────────────────


class TaskStore:
    """In-memory A2A task store.

    Concurrency: protected by an RLock. Reads and writes are atomic
    at the dict level. Persisted-to-disk variant TBD — for v1 a task
    lives only in the running process. The signed receipt IS persisted
    (via ReceiptStore), so the work-proof story survives restart even
    if the task itself does not.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        context_id: str,
        initial_message: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a new Task with state=SUBMITTED and one history entry."""
        task_id = uuid.uuid4().hex
        # Stamp the message with our IDs
        initial_message = dict(initial_message)
        initial_message["task_id"] = task_id
        initial_message["context_id"] = context_id
        if not initial_message.get("message_id"):
            initial_message["message_id"] = uuid.uuid4().hex

        task = {
            "id": task_id,
            "context_id": context_id,
            "status": {
                "state": TASK_STATE_SUBMITTED,
                "timestamp": now_ms(),
            },
            "artifacts": [],
            "history": [initial_message],
            "metadata": {},
        }
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._tasks.get(task_id)

    def append_message(
        self, task_id: str, message: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Append a message to an existing task's history."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            msg = dict(message)
            msg["task_id"] = task_id
            msg["context_id"] = task["context_id"]
            if not msg.get("message_id"):
                msg["message_id"] = uuid.uuid4().hex
            task["history"].append(msg)
            return task

    def set_state(
        self, task_id: str, state: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task["status"] = {
                "state": state,
                "timestamp": now_ms(),
            }
            return task

    def all_ids(self) -> List[str]:
        with self._lock:
            return list(self._tasks.keys())

    def set_metadata_key(
        self, task_id: str, key: str, value: Any,
    ) -> Optional[Dict[str, Any]]:
        """Set ``task.metadata[key] = value`` under the lock.

        F1 fix (2026-06-08): writing to the task dict directly from
        the handler was unsafe — handlers run on FastAPI's default
        thread pool, so an unprotected mutation could race with a
        concurrent ``append_message`` or ``set_state`` call to the
        same task. All writes that touch a stored task MUST go
        through a TaskStore method holding the RLock.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.setdefault("metadata", {})[key] = value
            return task


# ─── RPC handler ─────────────────────────────────────────────────────


class A2ARPCHandler:
    """Dispatch JSON-RPC requests to A2A method implementations.

    Each handler returns the JSON-RPC response dict. Errors map to
    JSON-RPC 2.0 error envelopes — the HTTP layer always returns 200
    with a structured body, per JSON-RPC convention.
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        receipt_store: Optional[ReceiptStore],
        identity: Optional["AgentIdentity"],
    ) -> None:
        self.tasks = task_store
        self.receipts = receipt_store
        self.identity = identity

    # ── public dispatch entry ─────────────────────────────────────

    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single JSON-RPC request.

        Returns the JSON-RPC 2.0 response envelope. Never raises —
        any exception becomes an INTERNAL_ERROR response so the
        client always sees structured JSON.
        """
        req_id = payload.get("id")  # may be None for notifications
        if payload.get("jsonrpc") != "2.0":
            return _rpc_error(
                req_id, JSONRPC_INVALID_REQUEST,
                'jsonrpc must be "2.0"',
            )
        method = payload.get("method", "")
        if not isinstance(method, str) or not method:
            return _rpc_error(
                req_id, JSONRPC_INVALID_REQUEST,
                "method must be a non-empty string",
            )
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "params must be an object",
            )

        try:
            if method == "message/send":
                return self._message_send(req_id, params)
            if method == "tasks/get":
                return self._tasks_get(req_id, params)
            if method == "tasks/cancel":
                return self._tasks_cancel(req_id, params)
            return _rpc_error(
                req_id, JSONRPC_METHOD_NOT_FOUND,
                f"unknown method: {method}",
                data={
                    "supported_methods": [
                        "message/send", "tasks/get", "tasks/cancel",
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("A2A RPC handler failed for method=%s", method)
            return _rpc_error(
                req_id, JSONRPC_INTERNAL_ERROR,
                "internal error processing request",
                data={"exc_type": type(exc).__name__},
            )

    # ── method: message/send ──────────────────────────────────────

    def _message_send(
        self, req_id: Any, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """A2A ``message/send``: create a Task or append to an existing one."""
        message = params.get("message")
        if not isinstance(message, dict):
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "params.message is required and must be an object",
            )

        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "message.parts must be a non-empty array",
            )
        if message.get("role") not in (ROLE_USER, ROLE_AGENT):
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                f"message.role must be {ROLE_USER!r} or {ROLE_AGENT!r}",
            )

        # Branch: existing Task (by task_id) or new context
        target_task_id = message.get("task_id") or ""
        if target_task_id:
            existing = self.tasks.get(target_task_id)
            if existing is None:
                return _rpc_error(
                    req_id, A2A_TASK_NOT_FOUND,
                    f"task {target_task_id} not found",
                )
            if existing["status"]["state"] in TERMINAL_STATES:
                return _rpc_error(
                    req_id, A2A_TASK_TERMINAL,
                    f"task {target_task_id} is terminal "
                    f"({existing['status']['state']}); cannot append",
                )
            task = self.tasks.append_message(target_task_id, message)
            # Update state to WORKING since a new message arrived
            assert task is not None  # we just verified existence
            task = self.tasks.set_state(target_task_id, TASK_STATE_WORKING)
            # F2 (2026-06-08): use ``step_started`` not ``goal_started``
            # for messages appended to an existing task — the goal is
            # already in progress; this message is the next step.
            self._emit_receipt_for_message(
                task, message, is_new_task=False,
            )
            return _rpc_result(req_id, task)

        # New context — generate IDs
        context_id = message.get("context_id") or uuid.uuid4().hex
        task = self.tasks.create(
            context_id=context_id, initial_message=message,
        )
        self._emit_receipt_for_message(
            task, task["history"][0], is_new_task=True,
        )
        return _rpc_result(req_id, task)

    # ── method: tasks/get ─────────────────────────────────────────

    def _tasks_get(
        self, req_id: Any, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_id = str(params.get("id", "") or "")
        if not task_id:
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "params.id is required",
            )
        task = self.tasks.get(task_id)
        if task is None:
            return _rpc_error(
                req_id, A2A_TASK_NOT_FOUND,
                f"task {task_id} not found",
            )
        return _rpc_result(req_id, task)

    # ── method: tasks/cancel ──────────────────────────────────────

    def _tasks_cancel(
        self, req_id: Any, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_id = str(params.get("id", "") or "")
        if not task_id:
            return _rpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                "params.id is required",
            )
        task = self.tasks.get(task_id)
        if task is None:
            return _rpc_error(
                req_id, A2A_TASK_NOT_FOUND,
                f"task {task_id} not found",
            )
        if task["status"]["state"] in TERMINAL_STATES:
            # Idempotent cancel — already terminal, just return current
            return _rpc_result(req_id, task)
        task = self.tasks.set_state(task_id, TASK_STATE_CANCELED)
        self._emit_receipt_for_cancel(task)
        return _rpc_result(req_id, task)

    # ── receipt emission (L1-1 integration) ───────────────────────

    def _emit_receipt_for_message(
        self,
        task: Dict[str, Any],
        message: Dict[str, Any],
        *,
        is_new_task: bool,
    ) -> None:
        """Sign a receipt for this message and link it to the task.

        F2 (2026-06-08): ``is_new_task=True`` → first timeline entry
        is ``goal_started`` (the task is born); ``is_new_task=False``
        → first entry is ``step_started`` (the goal already exists,
        this message is a step inside it). Emitting goal_started
        twice for the same goal_id would confuse motebit consumers
        building task-state machines off the timeline.
        """
        if self.identity is None or self.receipts is None:
            return
        try:
            # Build a minimal timeline — leading event then
            # nth.post_message. Future iterations may build richer
            # timelines as the task progresses through tool calls.
            leading_type = (
                TYPE_GOAL_STARTED if is_new_task else TYPE_STEP_STARTED
            )
            timeline = [
                TimelineEntry(
                    timestamp=now_ms(),
                    type=leading_type,
                    payload={
                        "task_id": task["id"],
                        "context_id": task["context_id"],
                    },
                ),
                TimelineEntry(
                    timestamp=now_ms(),
                    type=TYPE_NTH_POST_MESSAGE,
                    payload={
                        "message_id": message.get("message_id", ""),
                        "role": message.get("role", ""),
                        "parts_count": len(message.get("parts", [])),
                    },
                ),
            ]
            receipt = sign_receipt(
                timeline, self.identity, goal_id=task["id"],
            )
            self.receipts.save(receipt)
            # F1 (2026-06-08): metadata write goes through the store's
            # lock-protected setter, not a direct dict mutation. A
            # concurrent ``append_message`` on the same task would
            # otherwise race for the metadata slot.
            updated = self.tasks.set_metadata_key(
                task["id"], "nth_receipt_id", receipt["receipt_id"],
            )
            if updated is not None:
                # The task dict the caller has may be a stale snapshot
                # if a concurrent call mutated it; refresh in-place so
                # the JSON-RPC response carries the receipt_id.
                task["metadata"] = updated["metadata"]
        except Exception as exc:  # noqa: BLE001
            # Receipt emission must never break the request — log and
            # carry on. A task without a receipt is degraded but
            # functional.
            logger.warning(
                "receipt emission failed for task %s: %s",
                task["id"], exc,
            )

    def _emit_receipt_for_cancel(self, task: Dict[str, Any]) -> None:
        if self.identity is None or self.receipts is None:
            return
        try:
            timeline = [
                TimelineEntry(
                    timestamp=now_ms(),
                    type=TYPE_GOAL_COMPLETED,
                    payload={
                        "task_id": task["id"],
                        "result": "canceled",
                    },
                ),
            ]
            receipt = sign_receipt(
                timeline, self.identity, goal_id=task["id"],
            )
            self.receipts.save(receipt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cancel-receipt emission failed for task %s: %s",
                task["id"], exc,
            )
