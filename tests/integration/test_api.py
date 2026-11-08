"""Desktop route tests for authentication boundary inputs and review revisions."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from skill_layers import dashboard_api as plugin_api
from skill_layers.contracts import LayerError, ProfileScope

PROFILE = ProfileScope(
    "profile-1", Path("/tmp/profile"), Path("/tmp/profile/skills"), (), "context"
)


class ReviewService:
    revision = 1
    closed_count = 0
    mutations: list[tuple] = []

    def __init__(self, scope=None, readonly=False):
        self.scope = scope or PROFILE
        self.readonly = readonly

    def read(self, action, identifier=None, *, bounded=False):
        if action == "diff":
            return {
                "ok": True,
                "profile_id": self.scope.id,
                "profile": self.scope.public(),
                "data": {"candidate_id": identifier, "revision": type(self).revision},
            }
        return {"ok": True, "profile_id": self.scope.id, "profile": self.scope.public(), "data": {}}

    def mutate(self, action, parameters, *, expected_profile, request_id=None):
        type(self).mutations.append((action, parameters, expected_profile, request_id))
        if expected_profile != self.scope.id:
            return {
                "ok": False,
                "profile_id": self.scope.id,
                "profile": self.scope.public(),
                "error": {
                    "code": "PROFILE_MISMATCH",
                    "message": "stale",
                    "retryable": False,
                    "details": {},
                },
            }
        if action == "resolve" and parameters.get("revision") != type(self).revision:
            return {
                "ok": False,
                "profile_id": self.scope.id,
                "profile": self.scope.public(),
                "error": {
                    "code": "STALE_INPUT",
                    "message": "stale revision",
                    "retryable": True,
                    "details": {},
                },
            }
        if action == "resolve":
            type(self).revision += 1
            return {
                "ok": True,
                "profile_id": self.scope.id,
                "profile": self.scope.public(),
                "data": {"revision": type(self).revision},
            }
        if action == "publish" and parameters.get("review_hash") != "renewed-review":
            return {
                "ok": False,
                "profile_id": self.scope.id,
                "profile": self.scope.public(),
                "error": {
                    "code": "CONFLICTS_UNRESOLVED",
                    "message": "review required",
                    "retryable": False,
                    "details": {},
                },
            }
        return {
            "ok": True,
            "profile_id": self.scope.id,
            "profile": self.scope.public(),
            "data": {"action": action},
        }

    def close(self):
        type(self).closed_count += 1


def client(monkeypatch):
    ReviewService.revision = 1
    ReviewService.closed_count = 0
    ReviewService.mutations = []
    monkeypatch.setattr(plugin_api, "Service", ReviewService)
    monkeypatch.setattr(plugin_api, "current_scope", lambda: PROFILE)
    app = FastAPI()
    # This is the same router public Hermes mounts under /api/plugins/<name>.
    app.include_router(plugin_api.router, prefix="/api/plugins/skill-layers")
    return TestClient(app)


def test_mutations_require_profile_idempotency_and_reject_server_paths(monkeypatch):
    web = client(monkeypatch)
    missing = web.post("/api/plugins/skill-layers/derive", json={"source_id": "s", "label": "L"})
    assert missing.status_code == 422
    path = web.post(
        "/api/plugins/skill-layers/resolve",
        json={
            "candidate_id": "c",
            "revision": 1,
            "path": "SKILL.md",
            "take": "local",
            "server_path": "/etc/passwd",
            "expected_profile": "profile-1",
            "request_id": "r-1",
        },
    )
    assert path.status_code == 422
    assert path.json()["profile_id"] == "profile-1"
    assert path.json()["profile"]["id"] == "profile-1"
    assert ReviewService.mutations == []


def test_stream_body_limit_stops_before_buffering_more_than_cap(monkeypatch):
    monkeypatch.setattr(plugin_api, "current_scope", lambda: PROFILE)
    chunks = [b"a" * 100, b"b" * (plugin_api.MAX_API_REQUEST_BYTES + 1)]
    received = []

    async def receive():
        chunk = chunks.pop(0)
        received.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/derive"}, receive)
    response = anyio.run(plugin_api._mutate, request, "derive")
    assert response.status_code == 413
    body = json.loads(response.body)
    assert body["profile_id"] == "profile-1"
    assert body["error"]["code"] == "LIMIT_EXCEEDED"
    assert len(received) == 2


def test_constructor_layer_error_has_bound_profile_envelope(monkeypatch):
    monkeypatch.setattr(plugin_api, "current_scope", lambda: PROFILE)
    web = client(monkeypatch)

    class BrokenService:
        def __init__(self, *args, **kwargs):
            raise LayerError("STORAGE_ERROR", "Unsupported database schema.")

    monkeypatch.setattr(plugin_api, "Service", BrokenService)
    response = web.post(
        "/api/plugins/skill-layers/derive",
        json={
            "source_id": "source",
            "label": "Layer",
            "expected_profile": "profile-1",
            "request_id": "request-1",
        },
    )
    assert response.status_code == 503
    assert response.json()["profile_id"] == "profile-1"
    assert response.json()["profile"]["id"] == "profile-1"
    assert response.json()["error"]["code"] == "STORAGE_ERROR"


def test_read_tool_preserves_profile_errors_and_validation_codes(monkeypatch):
    from skill_layers import plugin as tool_plugin

    class BrokenService:
        def __init__(self, *args, **kwargs):
            raise LayerError("PROFILE_MISMATCH", "Stored data belongs to another profile.")

    monkeypatch.setattr(tool_plugin, "current_scope", lambda: PROFILE)
    monkeypatch.setattr(tool_plugin, "Service", BrokenService)
    mismatch = json.loads(tool_plugin._read_tool("list", {}))
    assert mismatch["profile_id"] == "profile-1"
    assert mismatch["error"]["code"] == "PROFILE_MISMATCH"

    malformed = json.loads(tool_plugin._read_tool("list", {"unexpected": True}))
    assert malformed["profile_id"] == "profile-1"
    assert malformed["error"]["code"] == "INVALID_STATE"


def test_service_lifecycle_runs_in_worker_with_request_context(monkeypatch):
    request_context = ContextVar("request_context", default="missing")
    main_thread = get_ident()
    traces: list[tuple[str, int, str]] = []

    class ThreadService:
        def __init__(self, scope=None, readonly=False):
            assert scope is PROFILE
            self.trace = []
            self._record("construct")

        def _record(self, event):
            observation = (event, get_ident(), request_context.get())
            self.trace.append(observation)
            traces.append(observation)

        def read(self, action, identifier=None, *, bounded=False):
            self._record("read")
            return {"ok": True, "profile_id": PROFILE.id, "profile": PROFILE.public(), "data": {}}

        def mutate(self, action, parameters, *, expected_profile, request_id=None):
            self._record("mutate")
            return {"ok": True, "profile_id": PROFILE.id, "profile": PROFILE.public(), "data": {}}

        def close(self):
            self._record("close")

    monkeypatch.setattr(plugin_api, "Service", ThreadService)
    monkeypatch.setattr(plugin_api, "current_scope", lambda: PROFILE)

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(
                {
                    "expected_profile": PROFILE.id,
                    "request_id": "request-1",
                    "source_id": "s",
                    "label": "L",
                }
            ).encode(),
            "more_body": False,
        }

    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/derive"}, receive)

    async def exercise():
        token = request_context.set("profile-request")
        try:
            await plugin_api._read("doctor")
            await plugin_api._mutate(request, "derive")
        finally:
            request_context.reset(token)

    anyio.run(exercise)
    assert {event for event, _, _ in traces} == {"construct", "read", "mutate", "close"}
    assert all(thread_id != main_thread for _, thread_id, _ in traces)
    assert all(value == "profile-request" for _, _, value in traces)
    assert len({thread_id for _, thread_id, _ in traces}) == 1
    assert plugin_api.MAX_API_RESPONSE_BYTES == 128 * 1024 * 1024


def test_unpaired_surrogate_is_a_validation_error(monkeypatch):
    web = client(monkeypatch)
    body = json.dumps(
        {
            "candidate_id": "candidate",
            "revision": 1,
            "path": "SKILL.md",
            "edited": "bad\ud800text",
            "expected_profile": "profile-1",
            "request_id": "request-1",
        },
        ensure_ascii=True,
    ).encode("ascii")
    response = web.post(
        "/api/plugins/skill-layers/resolve",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["profile_id"] == "profile-1"
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_conflict_and_revision_gate_requires_renewed_review(monkeypatch):
    web = client(monkeypatch)
    conflict = web.post(
        "/api/plugins/skill-layers/publish",
        json={
            "candidate_id": "c",
            "revision": 1,
            "review_hash": "old",
            "expected_profile": "profile-1",
            "request_id": "r1",
        },
    )
    assert conflict.status_code == 409

    stale = web.post(
        "/api/plugins/skill-layers/resolve",
        json={
            "candidate_id": "c",
            "revision": 0,
            "path": "SKILL.md",
            "take": "local",
            "expected_profile": "profile-1",
            "request_id": "r2",
        },
    )
    assert stale.status_code == 409
    assert web.get("/api/plugins/skill-layers/candidates/c").json()["data"]["revision"] == 1

    resolved = web.post(
        "/api/plugins/skill-layers/resolve",
        json={
            "candidate_id": "c",
            "revision": 1,
            "path": "SKILL.md",
            "edited_base64": "YQ==",
            "expected_profile": "profile-1",
            "request_id": "r3",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["revision"] == 2
    needs_new_review = web.post(
        "/api/plugins/skill-layers/publish",
        json={
            "candidate_id": "c",
            "revision": 2,
            "review_hash": "old",
            "expected_profile": "profile-1",
            "request_id": "r4",
        },
    )
    assert needs_new_review.status_code == 409
    published = web.post(
        "/api/plugins/skill-layers/publish",
        json={
            "candidate_id": "c",
            "revision": 2,
            "review_hash": "renewed-review",
            "expected_profile": "profile-1",
            "request_id": "r5",
        },
    )
    assert published.status_code == 200
    assert ReviewService.closed_count == 6
