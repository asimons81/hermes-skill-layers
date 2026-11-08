"""Independent fault-acceptance cases for publication, source loss, and restart.

All profiles and source trees are synthetic. Platform and Hermes host behavior are
not inferred from these unit tests.
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest
from test_service import V1, SyntheticDiscovery, act, derive, publish
from test_service import workspace as _workspace

import skill_layers.publish as publish_module
import skill_layers.service as service_module
from skill_layers.contracts import LayerError
from skill_layers.identity import read_identity
from skill_layers.service import Service


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Reuse test_service's synthetic profile while making its fixture local to this module."""
    generator = _workspace.__wrapped__(tmp_path, monkeypatch)
    value = next(generator)
    try:
        yield value
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_permission_denied_stage_is_reported_and_candidate_can_retry(
    workspace, monkeypatch: pytest.MonkeyPatch
):
    service, source, _scope = workspace
    first, previous = derive(service)
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]
    source_before = (source / "SKILL.md").read_bytes()
    old_export_before = (Path(previous["directory"]) / "SKILL.md").read_bytes()
    original_stage = service_module.stage_export

    def deny_stage(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "synthetic denied stage write")

    monkeypatch.setattr(service_module, "stage_export", deny_stage)
    denied = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert not denied["ok"]
    assert denied["error"]["code"] == "STORAGE_ERROR"
    assert denied["error"]["details"]["errno"] == errno.EACCES
    assert (source / "SKILL.md").read_bytes() == source_before
    assert (Path(previous["directory"]) / "SKILL.md").read_bytes() == old_export_before

    recovered = act(service, "recover", operation_id=denied["operation_id"])
    assert recovered["operations"][0]["state"] == "aborted_before_stage_commit"
    assert service.store.get("candidates", candidate["candidate_id"])["state"] == "ready"

    monkeypatch.setattr(service_module, "stage_export", original_stage)
    retried = publish(service, candidate["candidate_id"])
    assert Path(retried["directory"]).is_dir()
    assert Path(previous["directory"]).is_dir()
    assert (source / "SKILL.md").read_bytes() == source_before


def test_cross_device_rename_error_keeps_stage_for_recovery(
    workspace, monkeypatch: pytest.MonkeyPatch
):
    service, source, _scope = workspace
    first, previous = derive(service)
    candidate = act(service, "refresh", layer_id=first["layer_id"])
    review = service.read("diff", candidate["candidate_id"])["data"]
    old_export_before = (Path(previous["directory"]) / "SKILL.md").read_bytes()
    source_before = (source / "SKILL.md").read_bytes()
    original_rename = publish_module.rename_new

    def cross_device(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "synthetic cross-device rename")

    monkeypatch.setattr(publish_module, "rename_new", cross_device)
    refused = service.mutate(
        "publish",
        {
            "candidate_id": candidate["candidate_id"],
            "revision": review["revision"],
            "review_hash": review["review_hash"],
        },
        expected_profile=service.scope.id,
    )
    assert not refused["ok"]
    assert refused["error"]["code"] == "STORAGE_ERROR"
    assert refused["error"]["details"]["errno"] == errno.EXDEV
    operation = service.store.get("operations", refused["operation_id"])
    assert operation["phase"] == "staged"
    stage = Path(operation["stage"])
    target = Path(operation["destination"])
    assert stage.is_dir()
    assert not target.exists()
    assert (Path(previous["directory"]) / "SKILL.md").read_bytes() == old_export_before
    assert (source / "SKILL.md").read_bytes() == source_before

    monkeypatch.setattr(publish_module, "rename_new", original_rename)
    recovered = act(service, "recover", operation_id=refused["operation_id"])
    assert recovered["operations"][0]["state"] == "committed"
    assert target.is_dir()
    assert Path(previous["directory"]).is_dir()
    assert (source / "SKILL.md").read_bytes() == source_before


def test_missing_source_aborts_refresh_without_changing_preferred_export(workspace):
    service, source, _scope = workspace
    first, previous = derive(service)
    previous_bytes = (Path(previous["directory"]) / "SKILL.md").read_bytes()
    source_bytes = (source / "SKILL.md").read_bytes()
    export_count = len(service.store.list("exports"))
    candidate_count = len(service.store.list("candidates"))

    def missing(_source):
        raise LayerError("SOURCE_MOVED", "Synthetic discovery no longer resolves the source.")

    service.discovery.resolve = missing
    result = service.mutate(
        "refresh", {"layer_id": first["layer_id"]}, expected_profile=service.scope.id
    )

    assert not result["ok"]
    assert result["error"]["code"] == "SOURCE_MOVED"
    layer = service.store.get("layers", first["layer_id"])
    assert layer["preferred_export_id"] == previous["id"]
    assert len(service.store.list("exports")) == export_count
    assert len(service.store.list("candidates")) == candidate_count
    assert (Path(previous["directory"]) / "SKILL.md").read_bytes() == previous_bytes
    assert (source / "SKILL.md").read_bytes() == source_bytes


def test_service_restart_preserves_history_and_published_export(workspace):
    service, source, scope = workspace
    layer, export = derive(service)
    export_bytes = (Path(export["directory"]) / "SKILL.md").read_bytes()
    source_bytes = (source / "SKILL.md").read_bytes()
    service.close()

    reopened = Service(
        scope,
        discovery=SyntheticDiscovery(scope, source),
        scope_provider=lambda: scope,
    )
    try:
        listed = reopened.read("list")
        status = reopened.read("status", layer["layer_id"])
        assert listed["ok"] and status["ok"]
        assert len(listed["data"]["layers"]) == 1
        assert status["data"]["layer"]["preferred_export_id"] == export["id"]
        assert any(item["id"] == export["id"] for item in status["data"]["exports"])
        assert (Path(export["directory"]) / "SKILL.md").read_bytes() == export_bytes
        assert (source / "SKILL.md").read_bytes() == source_bytes == V1
    finally:
        reopened.close()


def test_removing_plugin_state_leaves_export_as_an_ordinary_skill(workspace):
    service, source, scope = workspace
    _layer, export = derive(service)
    export_path = Path(export["directory"])
    export_bytes = (export_path / "SKILL.md").read_bytes()
    source_bytes = (source / "SKILL.md").read_bytes()
    data_root = scope.home / "plugin-data" / "skill-layers"
    service.close()

    # Simulate uninstall cleanup of plugin-owned state only. This is not a Hermes uninstaller test.
    shutil.rmtree(data_root)

    assert not data_root.exists()
    assert export_path.is_dir()
    assert (export_path / "SKILL.md").read_bytes() == export_bytes
    assert read_identity((export_path / "SKILL.md").read_bytes()) == export["selector"]
    assert (source / "SKILL.md").read_bytes() == source_bytes == V1
