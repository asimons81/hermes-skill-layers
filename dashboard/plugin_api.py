"""Authenticated Desktop API routes for Skill Layers operator workflows."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from skill_layers.contracts import MAX_FILE_BYTES, LayerError, ProfileScope, Record
from skill_layers.service import Service
from skill_layers.sources import current_scope

router = APIRouter()
MAX_API_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_API_REQUEST_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4 + 64 * 1024

_READS = {
    "doctor": ("doctor", None),
    "sources": ("sources", None),
    "layers": ("list", None),
}
_MUTATION_FIELDS: dict[str, set[str]] = {
    "derive": {"source_id", "label"},
    "checkpoint": {"layer_id"},
    "refresh": {"layer_id"},
    "resolve": {"candidate_id", "revision", "path", "take", "edited", "edited_base64"},
    "publish": {"candidate_id", "revision", "review_hash"},
    "rollback": {"layer_id", "checkpoint_id"},
    "recover": {"operation_id"},
}


def _json(response: Record, status: int = 200) -> JSONResponse:
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_API_RESPONSE_BYTES:
        return JSONResponse(
            {
                "ok": False,
                "profile_id": response.get("profile_id"),
                "profile": response.get("profile"),
                "error": {
                    "code": "LIMIT_EXCEEDED",
                    "message": "The response exceeds the Desktop API limit.",
                    "retryable": False,
                    "details": {"max_response_bytes": MAX_API_RESPONSE_BYTES},
                },
            },
            status_code=413,
        )
    return JSONResponse(response, status_code=status)


async def _respond(response: Record, status: int = 200) -> JSONResponse:
    """Serialize large candidate trees off the shared event loop."""
    return await run_in_threadpool(_json, response, status)


def _status(response: Record) -> int:
    if response.get("ok"):
        return 200
    error = response.get("error") or {}
    code = error.get("code")
    if code == "NOT_FOUND":
        return 404
    if code == "PERMISSION_REQUIRED":
        return 403
    if code in {
        "CONFLICTS_UNRESOLVED",
        "STALE_INPUT",
        "PROFILE_MISMATCH",
        "SOURCE_MOVED",
        "RECOVERY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "DESTINATION_EXISTS",
        "IDENTITY_CHANGED",
    }:
        return 409
    if code in {"STORAGE_ERROR", "MERGE_ERROR", "MERGE_TIMEOUT", "DEPENDENCY_MISSING"}:
        return 503
    if code == "LIMIT_EXCEEDED":
        return 413
    return 422


def _error(
    message: str,
    code: str = "INVALID_STATE",
    *,
    scope: ProfileScope | None = None,
    error: LayerError | None = None,
) -> Record:
    return {
        "ok": False,
        "profile_id": scope.id if scope else None,
        "profile": scope.public() if scope else None,
        "error": error.to_dict()
        if error
        else {"code": code, "message": message, "retryable": False, "details": {}},
    }


def _read_in_worker(action: str, identifier: str | None, scope: ProfileScope) -> Record:
    service: Service | None = None
    try:
        service = Service(scope=scope, readonly=True)
        return service.read(action, identifier)
    except LayerError as exc:
        return _error(exc.message, scope=scope, error=exc)
    finally:
        if service is not None:
            service.close()


async def _read(action: str, identifier: str | None = None) -> JSONResponse:
    try:
        scope = current_scope()
    except LayerError as exc:
        response = _error(exc.message, scope=None, error=exc)
        return await _respond(response, _status(response))
    response = await run_in_threadpool(_read_in_worker, action, identifier, scope)
    return await _respond(response, _status(response))


def _mutate_in_worker(
    action: str,
    parameters: Record,
    expected_profile: str,
    request_id: str,
    scope: ProfileScope,
) -> Record:
    service: Service | None = None
    try:
        service = Service(scope=scope)
        return service.mutate(
            action,
            parameters,
            expected_profile=expected_profile,
            request_id=request_id,
        )
    except LayerError as exc:
        return _error(exc.message, scope=scope, error=exc)
    finally:
        if service is not None:
            service.close()


async def _mutate(request: Request, action: str) -> JSONResponse:
    try:
        scope = current_scope()
    except LayerError as exc:
        response = _error(exc.message, scope=None, error=exc)
        return await _respond(response, _status(response))
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_API_REQUEST_BYTES:
                response = _error(
                    "Request body exceeds the Desktop API limit.",
                    "LIMIT_EXCEEDED",
                    scope=scope,
                )
                return await _respond(response, _status(response))
            body.extend(chunk)
        payload = json.loads(body)
    except Exception:
        response = _error("Request body must be a JSON object.", scope=scope)
        return await _respond(response, 422)
    if not isinstance(payload, dict):
        response = _error("Request body must be a JSON object.", scope=scope)
        return await _respond(response, 422)
    allowed = _MUTATION_FIELDS[action] | {"expected_profile", "request_id"}
    if set(payload) - allowed:
        response = _error("Request contains unsupported fields.", scope=scope)
        return await _respond(response, 422)
    expected_profile = payload.get("expected_profile")
    request_id = payload.get("request_id")
    if not isinstance(expected_profile, str) or not expected_profile:
        response = _error("expected_profile is required.", scope=scope)
        return await _respond(response, 422)
    if not isinstance(request_id, str) or not request_id:
        response = _error("request_id is required for mutations.", scope=scope)
        return await _respond(response, 422)

    parameters = {
        key: value
        for key, value in payload.items()
        if key not in {"expected_profile", "request_id"}
    }
    if action == "resolve":
        choices = sum(key in parameters for key in ("take", "edited", "edited_base64"))
        if choices != 1:
            response = _error(
                "resolve requires exactly one of take, edited, or edited_base64.", scope=scope
            )
            return await _respond(response, 422)
        if "edited" in parameters:
            edited = parameters["edited"]
            if not isinstance(edited, str):
                response = _error("edited must be UTF-8 text.", scope=scope)
                return await _respond(response, 422)
            try:
                edited_size = len(edited.encode("utf-8"))
            except UnicodeEncodeError:
                response = _error("edited contains invalid Unicode text.", scope=scope)
                return await _respond(response, 422)
            if edited_size > MAX_FILE_BYTES:
                response = _error(
                    "edited must be UTF-8 text within the file limit.",
                    "LIMIT_EXCEEDED",
                    scope=scope,
                )
                return await _respond(
                    response,
                    _status(response),
                )
        if "edited_base64" in parameters:
            encoded = parameters["edited_base64"]
            if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
                response = _error(
                    "edited_base64 exceeds the file limit.", "LIMIT_EXCEEDED", scope=scope
                )
                return await _respond(response, _status(response))

    response = await run_in_threadpool(
        _mutate_in_worker, action, parameters, expected_profile, request_id, scope
    )
    return await _respond(response, _status(response))


@router.get("/doctor")
async def doctor() -> JSONResponse:
    return await _read("doctor")


@router.get("/sources")
async def sources() -> JSONResponse:
    return await _read("sources")


@router.get("/layers")
async def layers() -> JSONResponse:
    return await _read("list")


@router.get("/layers/{layer_id}")
async def layer_status(layer_id: str) -> JSONResponse:
    return await _read("status", layer_id)


@router.get("/candidates/{candidate_id}")
async def candidate_diff(candidate_id: str) -> JSONResponse:
    return await _read("diff", candidate_id)


def _mutation_endpoint(action: str) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> JSONResponse:
        return await _mutate(request, action)

    endpoint.__name__ = f"{action}_endpoint"
    return endpoint


for _action in _MUTATION_FIELDS:
    router.add_api_route(f"/{_action}", _mutation_endpoint(_action), methods=["POST"])
