"""Hermes plugin registrations for read-only agent views and the operator CLI."""

from __future__ import annotations

import json
from typing import Any

from .contracts import LayerError, ProfileScope
from .service import Service
from .sources import current_scope

MAX_TOOL_RESPONSE_BYTES = 32 * 1024

_TOOLS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "skill_layers_list",
        {
            "name": "skill_layers_list",
            "description": (
                "List saved Skill Layers and their preferred exports for the active profile."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        "list",
    ),
    (
        "skill_layers_status",
        {
            "name": "skill_layers_status",
            "description": "Read one Skill Layers layer, its exports, checkpoints, and candidates.",
            "parameters": {
                "type": "object",
                "properties": {"layer_id": {"type": "string", "description": "Layer UUID."}},
                "required": ["layer_id"],
                "additionalProperties": False,
            },
        },
        "status",
    ),
    (
        "skill_layers_diff",
        {
            "name": "skill_layers_diff",
            "description": (
                "Read a bounded review diff and publish receipt for a Skill Layers candidate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "description": "Candidate UUID."}
                },
                "required": ["candidate_id"],
                "additionalProperties": False,
            },
        },
        "diff",
    ),
)


def _encode_response(response: dict[str, Any]) -> str:
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES:
        return encoded
    return json.dumps(
        {
            "ok": False,
            "profile_id": response.get("profile_id"),
            "profile": response.get("profile"),
            "error": {
                "code": "LIMIT_EXCEEDED",
                "message": (
                    "The read-only tool response exceeds 32 KiB; use the CLI or Desktop review."
                ),
                "retryable": False,
                "details": {"max_response_bytes": MAX_TOOL_RESPONSE_BYTES},
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _read_tool(action: str, arguments: dict[str, Any]) -> str:
    service: Service | None = None
    try:
        scope = current_scope()
    except LayerError as exc:
        return _tool_error(exc.code, exc.message, None, error=exc)
    try:
        identifier: str | None = None
        if not isinstance(arguments, dict):
            raise TypeError("Expected an object of tool arguments.")
        if action == "list":
            if arguments:
                raise TypeError("The list tool does not accept arguments.")
        elif action == "status":
            if set(arguments) != {"layer_id"}:
                raise TypeError("status accepts only layer_id.")
            identifier = arguments.get("layer_id")
            if not isinstance(identifier, str):
                raise TypeError("layer_id must be a string.")
        elif action == "diff":
            if set(arguments) != {"candidate_id"}:
                raise TypeError("diff accepts only candidate_id.")
            identifier = arguments.get("candidate_id")
            if not isinstance(identifier, str):
                raise TypeError("candidate_id must be a string.")
        else:
            raise ValueError("Unsupported read-only action.")
        service = Service(scope=scope, readonly=True)
        if action == "list":
            response = service.read("list", bounded=True)
        elif action == "status":
            response = service.read("status", identifier, bounded=True)
        else:
            response = service.read("diff", identifier, bounded=True)
        return _encode_response(response)
    except LayerError as exc:
        return _tool_error(exc.code, exc.message, scope, error=exc)
    except (TypeError, ValueError) as exc:
        return _tool_error("INVALID_STATE", str(exc), scope)
    except Exception:
        return _tool_error(
            "STORAGE_ERROR",
            "The read-only Skill Layers request could not be completed.",
            service.scope if service is not None else scope,
        )
    finally:
        if service is not None:
            service.close()


def _list_tool(arguments: dict[str, Any], **_kwargs: Any) -> str:
    return _read_tool("list", arguments)


def _status_tool(arguments: dict[str, Any], **_kwargs: Any) -> str:
    return _read_tool("status", arguments)


def _diff_tool(arguments: dict[str, Any], **_kwargs: Any) -> str:
    return _read_tool("diff", arguments)


def _tool_error(
    code: str,
    message: str,
    scope: ProfileScope | None,
    *,
    error: LayerError | None = None,
) -> str:
    return _encode_response(
        {
            "ok": False,
            "profile_id": scope.id if scope else None,
            "profile": scope.public() if scope else None,
            "error": error.to_dict()
            if error
            else {"code": code, "message": message, "retryable": False, "details": {}},
        }
    )


def register(ctx: Any) -> None:
    """Register three read-only tools and the operator's Hermes CLI command tree."""
    handlers = {
        "skill_layers_list": _list_tool,
        "skill_layers_status": _status_tool,
        "skill_layers_diff": _diff_tool,
    }
    for name, schema, _action in _TOOLS:
        ctx.register_tool(name=name, toolset="skill_layers", schema=schema, handler=handlers[name])

    from .cli import handle_cli, setup_cli

    ctx.register_cli_command(
        name="skill-layers",
        help="Manage preserved skill sources and reviewed derivatives.",
        description="Inspect, derive, reconcile, checkpoint, and publish Skill Layers.",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
    )
