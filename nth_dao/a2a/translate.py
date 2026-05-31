"""A2A ↔ NTH DAO translation primitives (v0.9.5 starter).

Reference: https://a2a-protocol.org/latest/specification/

These are pure data transformations. No I/O, no HTTP, no server. The
v0.10.0 adapter package will compose them with FastAPI/JSON-RPC.

Mapping at a glance:

    NTH DAO term         A2A term
    ─────────────────    ───────────────────
    AgentRegistry rec    Agent Card (`/.well-known/agent.json`)
    MissionTemplate      Skill (inside Agent Card .skills array)
    Mission              Task
    MissionStep          (no exact equivalent — A2A is flatter)
    AgentIdentity DID    Agent Card .id ("did:key:..." or "did:web:...")
    Endorsement          (no direct equivalent — A2A relies on OAuth)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ─────────────────── template → A2A Skill ───────────────────


def _iofield_to_jsonschema(io_field: Dict[str, Any]) -> Dict[str, Any]:
    """Convert our IOField dict to a JSON Schema fragment (A2A's preferred form)."""
    out: Dict[str, Any] = {"description": io_field.get("description", "")}
    t = io_field.get("type", "string")
    if t == "string":
        out["type"] = "string"
    elif t == "int":
        out["type"] = "integer"
    elif t == "float":
        out["type"] = "number"
    elif t == "bool":
        out["type"] = "boolean"
    elif t == "enum":
        out["type"] = "string"
        out["enum"] = list(io_field.get("values", []))
    elif t == "json":
        out["type"] = "object"
    else:
        out["type"] = "string"
    if io_field.get("default") not in (None, "", []):
        out["default"] = io_field["default"]
    return out


def template_to_a2a_skill(template) -> Dict[str, Any]:
    """Render a MissionTemplate as one A2A Skill (entry in AgentCard.skills).

    Args:
        template: MissionTemplate (object or dict). If a dict is given, it
                  is read directly; if an object, it must expose `to_dict()`.

    Returns:
        A dict shaped like A2A's Skill object.
    """
    if hasattr(template, "to_dict"):
        td = template.to_dict()
    elif isinstance(template, dict):
        td = template
    else:
        raise TypeError(
            f"template must be MissionTemplate or dict; got {type(template)}"
        )

    inputs_schema = {
        "type": "object",
        "properties": {
            name: _iofield_to_jsonschema(field_dict)
            for name, field_dict in (td.get("inputs") or {}).items()
        },
        "required": [
            name
            for name, field_dict in (td.get("inputs") or {}).items()
            if field_dict.get("required", False)
        ],
    }
    outputs_schema = {
        "type": "object",
        "properties": {
            name: _iofield_to_jsonschema(field_dict)
            for name, field_dict in (td.get("outputs") or {}).items()
        },
    }
    return {
        "id":          f"{td.get('template_id', '')}@{td.get('version', '')}",
        "name":        td.get("name", ""),
        "description": td.get("description", ""),
        "tags":        list(td.get("tags") or []),
        # A2A doesn't have a "category" field per se; tags are the closest.
        # We mirror category as a tag prefix so it's discoverable.
        "category":    td.get("category", "general"),
        "input_schema":  inputs_schema,
        "output_schema": outputs_schema,
        # NTH DAO-specific extension: keep the publisher_did so an A2A
        # client can choose to trust based on did:key identity.
        "x-nth-dao": {
            "publisher_did":  td.get("publisher_did", ""),
            "template_type":  td.get("template_type", "agent_task"),
            "version":        td.get("version", ""),
            "suggested_reward": td.get("suggested_reward", 0.0),
        },
    }


# ─────────────────── AgentCard assembly ───────────────────


def agent_card_from(
    *,
    agent_did: str,
    name: str,
    description: str = "",
    templates: Optional[List[Any]] = None,
    capabilities: Optional[List[str]] = None,
    endpoint_url: str = "",
    version: str = "1.0.0",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble an A2A AgentCard suitable for `/.well-known/agent.json`.

    Args:
        agent_did:    DID identifying the agent (e.g. "did:key:z6Mk...").
        name:         Human-readable agent name.
        description:  Markdown description.
        templates:    iterable of MissionTemplate objects whose skill schemas
                      should be advertised. Each is rendered via
                      `template_to_a2a_skill`.
        capabilities: tags / capability strings.
        endpoint_url: where this agent listens (the future adapter's HTTP URL).
        version:      AgentCard's `version` field (NOT our package version).
    """
    skills = [template_to_a2a_skill(t) for t in (templates or [])]
    return {
        "id":           agent_did,
        "name":         name,
        "description":  description,
        "version":      version,
        "endpoint":     endpoint_url,
        "capabilities": list(capabilities or []),
        "skills":       skills,
        "metadata":     dict(metadata or {}),
        # Make the file recognizable as A2A-shaped without a content-type negotiation
        "schema":       "https://a2a-protocol.org/schemas/agent-card-v1.json",
        "x-nth-dao":    {"version": version, "wire_format": "nth-dao-agent-card-v1"},
    }


# ─────────────────── Mission ↔ A2A Task ───────────────────


def a2a_task_from_mission(mission) -> Dict[str, Any]:
    """Render one of our Missions as an A2A Task object.

    A Task in A2A carries: id, status, input, output (when done), and a
    history of state transitions.
    """
    if hasattr(mission, "to_dict"):
        md = mission.to_dict()
    elif isinstance(mission, dict):
        md = mission
    else:
        raise TypeError(f"mission must be Mission or dict; got {type(mission)}")

    # A2A states are submitted / in_progress / completed / failed / cancelled.
    # Map NTH DAO state machine:
    NTH_TO_A2A = {
        "planning":  "submitted",
        "active":    "in_progress",
        "paused":    "submitted",
        "completed": "completed",
        "failed":    "failed",
        "cancelled": "cancelled",
    }
    nth_status = md.get("status", "planning")
    a2a_status = NTH_TO_A2A.get(nth_status, "submitted")

    # Aggregate step-level inputs/outputs.
    steps = md.get("steps", []) or []
    aggregated_inputs: Dict[str, Any] = {}
    aggregated_outputs: Dict[str, Any] = {}
    for s in steps:
        if isinstance(s, dict):
            aggregated_inputs.update(s.get("inputs", {}) or {})
            if s.get("output"):
                aggregated_outputs[s.get("id", "")] = s["output"]

    history = [
        {
            "type":      "step_transition",
            "step_id":   (s if isinstance(s, dict) else {}).get("id", ""),
            "status":    (s if isinstance(s, dict) else {}).get("status", ""),
            "timestamp": (s if isinstance(s, dict) else {}).get("updated_at", ""),
        }
        for s in steps
    ]

    return {
        "id":           md.get("id", ""),
        "status":       a2a_status,
        "title":        md.get("title", ""),
        "description":  md.get("goal", ""),
        "input":        aggregated_inputs,
        "output":       aggregated_outputs,
        "created_at":   md.get("created_at", ""),
        "updated_at":   md.get("updated_at", ""),
        "completed_at": md.get("completed_at"),
        "history":      history,
        "x-nth-dao": {
            "owner":            md.get("owner", ""),
            "template_id":      md.get("template_id"),
            "template_version": md.get("template_version"),
            "template_lock":    md.get("template_lock", {}),
        },
    }


# ─────────────────── inbound A2A → NTH DAO inputs ───────────────────


def mission_inputs_from_a2a_message(
    a2a_message: Dict[str, Any],
    template,
) -> Dict[str, Any]:
    """Extract our `inputs` dict from an A2A SendMessage payload.

    A2A SendMessage typically has shape:

        {"jsonrpc": "2.0", "method": "SendMessage",
         "params": {"task_id": "...", "skill_id": "...",
                    "input": { ... }}}

    or with simpler bindings just:

        {"input": { ... }}

    We accept either form and pull out the `input` dict, then validate it
    against the template's input schema.

    Raises:
        ValueError on missing/invalid inputs.
    """
    if hasattr(template, "validate_inputs"):
        validator = template.validate_inputs
    else:
        validator = None

    # Drill into common A2A shapes
    if not isinstance(a2a_message, dict):
        raise ValueError("A2A message must be a JSON object")
    if "params" in a2a_message and isinstance(a2a_message["params"], dict):
        params = a2a_message["params"]
        inputs = params.get("input") if isinstance(params.get("input"), dict) else {}
    elif "input" in a2a_message:
        inputs = a2a_message["input"] if isinstance(a2a_message["input"], dict) else {}
    else:
        inputs = {}

    if validator is not None:
        err = validator(inputs)
        if err:
            raise ValueError(f"A2A inputs invalid: {err}")

    return dict(inputs)
