"""Behavior contracts for source preservation and interrupted publication."""

from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

from skill_layers.contracts import ProfileScope, Source
from skill_layers.identity import read_identity
from skill_layers.service import Service

V1 = (
    b"---\nname: release-notes\ndescription: Synthetic release notes.\n---\n"
    b"# Notes\n\nOverview.\n\nInclude changes.\n\nClose.\n"
)
V2 = V1.replace(b"Close.", b"Include issue links.")


class SyntheticDiscovery:
    def __init__(self, scope: ProfileScope, directory: Path) -> None:
        self.scope, self.directory = scope, directory
        self.source = Source(
            "opaque-source",
            "release-notes",
            directory.parent,
            directory.name,
            "release-notes",
            scope.discovery_context,
        )

    def sources(self) -> list[Source]:
        return [self.source]

    def lookup(self, identifier: str) -> Source:
        assert identifier == self.source.id
        return self.source

    def resolve(self, source: Source) -> Path:
        assert source.id == self.source.id
        return self.directory

    def verify_export(self, selector: str, directory: Path) -> None:
        assert read_identity((directory / "SKILL.md").read_bytes()) == selector


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "profile"
    source = tmp_path / "sources" / "release-notes"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(V1)
    (source / "references").mkdir()
    (source / "references" / "guide.txt").write_bytes(b"Support survives.\r\n")
    scope = ProfileScope(
        "test-profile",
        home,
        home / "skills",
        ((1, source.parent), (2, home / "skills")),
        "test-context",
    )
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name: str) -> Path:
        assert name == "skill-layers"
        target = home / "plugin-data" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    storage.plugin_data_dir = plugin_data_dir
    plugins = types.ModuleType("plugins")
    plugins.__path__ = []
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    service = Service(
        scope, discovery=SyntheticDiscovery(scope, source), scope_provider=lambda: scope
    )
    yield service, source, scope
    service.close()


def act(service: Service, action: str, **parameters):
    response = service.mutate(action, parameters, expected_profile=service.scope.id)
    assert response["ok"], response
    return response["data"]


def publish(service: Service, candidate_id: str):
    review = service.read("diff", candidate_id)
    assert review["ok"], review
    data = review["data"]
    return act(
        service,
        "publish",
        candidate_id=candidate_id,
        revision=data["revision"],
        review_hash=data["review_hash"],
    )["export"]


def derive(service: Service):
    candidate = act(service, "derive", source_id="opaque-source", label="Release notes")
    return candidate, publish(service, candidate["candidate_id"])


def test_full_preserving_reconciliation_and_rollback(workspace):
    service, source, _ = workspace
    source_hash = hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()
    first, original = derive(service)
    original_dir = Path(original["directory"])
    assert read_identity((original_dir / "SKILL.md").read_bytes()) == original["selector"]
    assert (original_dir / "references" / "guide.txt").read_bytes() == b"Support survives.\r\n"
    assert hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest() == source_hash
    local = (
        (original_dir / "SKILL.md")
        .read_bytes()
        .replace(b"Include changes.", b"Include migration notes.")
    )
    (original_dir / "SKILL.md").write_bytes(local)
    checkpoint = act(service, "checkpoint", layer_id=first["layer_id"])["checkpoint"]
    (source / "SKILL.md").write_bytes(V2)
    refreshed = act(service, "refresh", layer_id=first["layer_id"])
    combined = publish(service, refreshed["candidate_id"])
    combined_text = (Path(combined["directory"]) / "SKILL.md").read_bytes()
    assert b"Include migration notes." in combined_text
    assert b"Include issue links." in combined_text
    base = service.store.load_snapshot(combined["base_snapshot"])
    assert base.tree["SKILL.md"].content == V2
    assert (original_dir / "SKILL.md").read_bytes() == local
    assert (source / "SKILL.md").read_bytes() == V2
    rollback = act(service, "rollback", layer_id=first["layer_id"], checkpoint_id=checkpoint["id"])
    rolled = publish(service, rollback["candidate_id"])
    rolled_text = (Path(rolled["directory"]) / "SKILL.md").read_bytes()
    assert b"Include migration notes." in rolled_text and b"Include issue links." not in rolled_text
    assert rolled["base_snapshot"] == checkpoint["base_snapshot"]
    assert len({original["selector"], combined["selector"], rolled["selector"]}) == 3
    assert (original_dir / "SKILL.md").read_bytes() == local
    assert (Path(combined["directory"]) / "SKILL.md").read_bytes() == combined_text


def test_recorded_conflict_and_review_hash_gate(workspace):
    service, source, _ = workspace
    first, export = derive(service)
    path = Path(export["directory"]) / "SKILL.md"
    path.write_bytes(path.read_bytes().replace(b"# Notes", b"# Local"))
    (source / "SKILL.md").write_bytes(V1.replace(b"# Notes", b"# Upstream"))
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]
    refused = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert refused["error"]["code"] == "CONFLICTS_UNRESOLVED"
    resolved = act(
        service,
        "resolve",
        candidate_id=candidate["candidate_id"],
        revision=review["revision"],
        path="SKILL.md",
        take="local",
    )
    obsolete = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": resolved["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert obsolete["error"]["code"] == "STALE_INPUT"
    published = publish(service, candidate["candidate_id"])
    assert b"# Local" in (Path(published["directory"]) / "SKILL.md").read_bytes()


def test_stale_edit_before_check_and_later_edit_survives(workspace):
    service, _, _ = workspace
    first, export = derive(service)
    current_path = Path(export["directory"]) / "SKILL.md"
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    before = current_path.read_bytes()
    current_path.write_bytes(before + b"\nEarly edit.\n")
    review = service.read("diff", candidate["candidate_id"])["data"]
    refused = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert refused["error"]["code"] == "STALE_INPUT"
    assert current_path.read_bytes() == before + b"\nEarly edit.\n"
    retry = act(service, "refresh", layer_id=first["layer_id"])
    service.hooks["after_final_check"] = lambda: current_path.write_bytes(
        before + b"\nLater edit.\n"
    )
    fresh = publish(service, retry["candidate_id"])
    assert current_path.read_bytes() == before + b"\nLater edit.\n"
    result = (Path(fresh["directory"]) / "SKILL.md").read_bytes()
    assert b"Early edit." in result and b"Later edit." not in result


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "point", ["before_stage", "before_rename", "after_rename", "after_db_commit"]
)
def test_crash_recovery_preserves_source_and_prior_exports(workspace, point):
    service, source, _ = workspace
    first, old = derive(service)
    old_bytes = (Path(old["directory"]) / "SKILL.md").read_bytes()
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]

    def crash():
        raise SimulatedCrash()

    service.hooks[point] = crash
    with pytest.raises(SimulatedCrash):
        service.mutate(
            "publish",
            {
                "candidate_id": candidate["candidate_id"],
                "revision": review["revision"],
                "review_hash": review["review_hash"],
            },
            expected_profile=service.scope.id,
        )
    service.hooks.clear()
    result = service.mutate("recover", {}, expected_profile=service.scope.id)
    if point == "before_stage":
        assert result["error"]["code"] == "RECOVERY_REQUIRED"
    else:
        assert result["ok"], result
        latest = service.store.get("layers", first["layer_id"])
        assert latest["preferred_export_id"] != old["id"]
    assert (source / "SKILL.md").read_bytes() == V1
    assert (Path(old["directory"]) / "SKILL.md").read_bytes() == old_bytes


def test_duplicate_request_and_profile_mismatch(workspace):
    service, _, _ = workspace
    request_id = "a1f15a43-d3ae-4472-93c2-9383504516c4"
    parameters = {"source_id": "opaque-source", "label": "Example"}
    first = service.mutate(
        "derive", parameters, expected_profile=service.scope.id, request_id=request_id
    )
    repeated = service.mutate(
        "derive", parameters, expected_profile=service.scope.id, request_id=request_id
    )
    assert first == repeated and first["ok"]
    changed = service.mutate(
        "derive",
        {**parameters, "label": "Other"},
        expected_profile=service.scope.id,
        request_id=request_id,
    )
    assert changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    wrong = service.mutate("derive", parameters, expected_profile="another-profile")
    assert wrong["error"]["code"] == "PROFILE_MISMATCH"
    assert len(service.store.list("layers")) == 1


def test_offline_workflow_and_read_only_views(workspace, monkeypatch):
    import socket

    service, source, scope = workspace

    def forbidden(*args, **kwargs):
        raise AssertionError("Plugin-owned network attempt")

    monkeypatch.setattr(socket, "socket", forbidden)
    first, initial = derive(service)
    local_path = Path(initial["directory"]) / "SKILL.md"
    local_path.write_bytes(
        local_path.read_bytes().replace(b"Include changes.", b"Include migrations.")
    )
    checkpoint = act(service, "checkpoint", layer_id=first["layer_id"])["checkpoint"]
    (source / "SKILL.md").write_bytes(V2)
    updated = act(service, "refresh", layer_id=first["layer_id"])
    publish(service, updated["candidate_id"])
    rollback = act(service, "rollback", layer_id=first["layer_id"], checkpoint_id=checkpoint["id"])
    publish(service, rollback["candidate_id"])
    service.close()

    def state():
        return {
            str(path.relative_to(scope.home)): path.read_bytes()
            for path in scope.home.rglob("*")
            if path.is_file()
        }

    before = state()
    reader = Service(
        scope, readonly=True, discovery=service.discovery, scope_provider=lambda: scope
    )
    try:
        assert reader.read("list", bounded=True)["ok"]
        assert reader.read("status", first["layer_id"], bounded=True)["ok"]
        assert reader.read("diff", updated["candidate_id"], bounded=True)["ok"]
    finally:
        reader.close()
    assert state() == before


def test_destination_created_after_check_is_never_replaced(workspace):
    service, _, _ = workspace
    first, old = derive(service)
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]
    target = Path(review["destination"])
    service.hooks["after_final_check"] = lambda: target.mkdir()
    result = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert result["error"]["code"] == "DESTINATION_EXISTS"
    assert target.is_dir() and not list(target.iterdir())
    assert Path(old["directory"]).is_dir()


def test_interrupted_prepare_retry_returns_committed_result(workspace):
    service, _, _ = workspace
    request_id = "5adbf067-98fc-4f1d-be36-ef1518b16b44"

    def crash():
        raise SimulatedCrash()

    service.hooks["after_prepare_commit"] = crash
    parameters = {"source_id": "opaque-source", "label": "Retry after commit"}
    with pytest.raises(SimulatedCrash):
        service.mutate(
            "derive", parameters, expected_profile=service.scope.id, request_id=request_id
        )
    service.hooks.clear()
    result = service.mutate(
        "derive", parameters, expected_profile=service.scope.id, request_id=request_id
    )
    assert result["ok"]
    assert len(service.store.list("layers")) == 1
    assert len(service.store.list("candidates")) == 1
    assert (
        result
        == next(
            item for item in service.store.list("operations") if item["request_id"] == request_id
        )["result"]
    )


def test_concurrent_publications_choose_one_preferred_export(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, source, scope = workspace
    first, original = derive(service)
    candidates = [act(service, "refresh", layer_id=first["layer_id"]) for _ in range(2)]
    reviews = [service.read("diff", row["candidate_id"])["data"] for row in candidates]
    barrier = Barrier(2, timeout=10)

    def publish_one(review):
        worker = Service(
            scope,
            discovery=SyntheticDiscovery(scope, source),
            scope_provider=lambda: scope,
            hooks={"before_final_check": lambda: barrier.wait()},
        )
        try:
            return worker.mutate(
                "publish",
                {
                    "candidate_id": review["candidate_id"],
                    "revision": review["revision"],
                    "review_hash": review["review_hash"],
                },
                expected_profile=scope.id,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish_one, reviews))
    assert sum(result["ok"] for result in outcomes) == 1, outcomes
    rejected = next(result for result in outcomes if not result["ok"])
    assert rejected["error"]["code"] == "STALE_INPUT"
    layer = service.store.get("layers", first["layer_id"])
    assert (
        layer["preferred_export_id"]
        == next(result for result in outcomes if result["ok"])["data"]["export"]["id"]
    )
    assert len(service.store.list("exports")) == 2
    assert Path(original["directory"]).is_dir()
    assert (source / "SKILL.md").read_bytes() == V1


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory-swap race; Windows holds deny-delete handles"
)
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_destination_parent_swap_fails_without_outside_write(workspace, replacement):
    service, source, scope = workspace
    first, old = derive(service)
    original_bytes = (Path(old["directory"]) / "SKILL.md").read_bytes()
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]
    outside = scope.home.parent / "outside"
    outside.mkdir()
    moved = scope.home / "original-skills"

    def swap():
        scope.skills_dir.rename(moved)
        if replacement == "symlink":
            scope.skills_dir.symlink_to(outside, target_is_directory=True)
        else:
            scope.skills_dir.mkdir()

    service.hooks["after_final_check"] = swap
    result = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=scope.id,
    )
    assert not result["ok"]
    assert result["error"]["code"] == "PROFILE_MISMATCH"
    assert list(outside.iterdir()) == []
    if replacement == "directory":
        assert list(scope.skills_dir.iterdir()) == []
    assert sorted(path.name for path in moved.iterdir()) == [Path(old["directory"]).name]
    assert (moved / Path(old["directory"]).name / "SKILL.md").read_bytes() == original_bytes
    assert (source / "SKILL.md").read_bytes() == V1
