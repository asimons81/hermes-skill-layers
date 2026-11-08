"""Tests for QA isolation and golden evidence validation (no real host is mocked as passed)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "skill_layers_qa_runner", Path(__file__).parents[2] / "scripts" / "qa.py"
)
assert SPEC and SPEC.loader
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks are unavailable on this host: {exc}")


def test_finite_payload_excludes_project_state_and_python_caches(tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    for filename in qa.PAYLOAD_FILES:
        (source / filename).write_text(filename, encoding="utf-8")
    for directory in qa.PAYLOAD_DIRS:
        folder = source / directory
        folder.mkdir()
        (folder / "__init__.py").write_text("# synthetic\n", encoding="utf-8")
        cache = folder / "__pycache__"
        cache.mkdir()
        (cache / "bad.pyc").write_bytes(b"cache")
    (source / "skill_layers" / "__init__.py").write_text("# finite package\n", encoding="utf-8")
    (source / "work").mkdir()
    (source / "work" / "secret.txt").write_text("must not copy", encoding="utf-8")
    (source / ".git").mkdir()
    monkeypatch.setattr(qa, "ROOT", source)

    target = tmp_path / "payload"
    qa.copy_finite_payload(target)

    assert (target / "skill_layers" / "__init__.py").is_file()
    assert not (target / "work").exists()
    assert not (target / ".git").exists()
    assert not list(target.rglob("*.pyc"))


def test_environment_roots_are_child_only_and_not_project_links(tmp_path, monkeypatch):
    before = dict(__import__("os").environ)
    monkeypatch.setattr(qa, "os", __import__("os"))
    profile = tmp_path / "profile"
    profile.mkdir()
    env = qa.clean_environment(
        profile, tmp_path / "home", tmp_path / "runtime", tmp_path / "uv-cache"
    )
    assert env["HERMES_HOME"] == str(profile)
    assert env["HOME"] == str(tmp_path / "home")
    assert "PYTHONPATH" not in env
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache")
    assert dict(__import__("os").environ) == before
    assert not profile.is_symlink()


def test_profile_setup_uses_public_command_and_shared_install_root(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, *, cwd, env, capture_output, text, timeout):
        commands.append((command, cwd, env["HERMES_HOME"], env["HERMES_RUNTIME_DIR"]))
        name = command[command.index("create") + 1]
        profile = Path(env["HERMES_HOME"]) / "profiles" / name
        profile.mkdir(parents=True)
        (profile / ".env").write_text("", encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    monkeypatch.setattr(qa, "host_command", lambda _root, command, **_kwargs: command)
    runtime = tmp_path / "run" / "task-runtime"
    uv_cache = tmp_path / "run" / "uv-cache"
    home, os_home, runtime = qa.create_named_profiles(
        hermes_root=tmp_path / "host",
        run_root=tmp_path / "run",
        runtime=runtime,
        uv_cache=uv_cache,
        names=("profile-a", "profile-b"),
    )
    assert (home / "profiles" / "profile-a" / ".env").is_file()
    assert (home / "profiles" / "profile-b" / ".env").is_file()
    assert os_home.is_dir() and runtime.is_dir()
    assert uv_cache.is_dir()
    assert len(commands) == 2
    assert all(command[0:3] == ["python", "-m", "hermes_cli.main"] for command, *_ in commands)
    assert len({runtime_path for _, _, _, runtime_path in commands}) == 1


def test_runtime_cache_is_host_pinned_task_owned_and_reusable(tmp_path, monkeypatch):
    project = tmp_path / "workspace" / "work" / "release-candidate"
    artifacts = project / "work" / "artifacts"
    artifacts.mkdir(parents=True)
    monkeypatch.setattr(qa, "ROOT", project)
    cache, uv_cache, record = qa.prepare_runtime_cache(
        artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460"
    )
    assert cache.is_dir() and uv_cache.is_dir()
    assert cache.parent == artifacts / ".qa-runtime"
    assert record["seed_topology"] == "created-empty"
    assert record["canonical_path"] == str(cache.resolve())

    prepared = project / "work" / "host-runtime"
    prepared.mkdir(parents=True)
    cache.rmdir()
    _directory_symlink_or_skip(cache, prepared)
    reused, _, record = qa.prepare_runtime_cache(
        artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460"
    )
    assert reused.resolve() == prepared.resolve()
    assert record["seed_topology"] == "symlink"

    cache.unlink()
    unsafe = tmp_path / "workspace" / "work" / "host-runtime"
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.mkdir()
    _directory_symlink_or_skip(cache, unsafe)
    with pytest.raises(qa.QAError, match="must target this task's"):
        qa.prepare_runtime_cache(artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460")


def test_runtime_cache_parent_symlink_cannot_redirect_outside_artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_symlink_or_skip(artifacts / ".qa-runtime", outside)

    with pytest.raises(qa.QAError, match="runtime cache parent must be a real"):
        qa.prepare_runtime_cache(artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460")


def test_uv_cache_symlink_cannot_redirect_outside_cache_parent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    artifacts = project / "work" / "artifacts"
    artifacts.mkdir(parents=True)
    monkeypatch.setattr(qa, "ROOT", project)
    _runtime, uv_cache, _record = qa.prepare_runtime_cache(
        artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460"
    )
    uv_cache.rmdir()
    outside = tmp_path / "outside-uv-cache"
    outside.mkdir()
    _directory_symlink_or_skip(uv_cache, outside)

    with pytest.raises(qa.QAError, match="UV cache must be a real"):
        qa.prepare_runtime_cache(artifacts, "c8d4e43efd296f632116cf3f17738aa2c4838460")


def test_pin_verification_requires_the_exact_host_module_layout(tmp_path, monkeypatch):
    host = tmp_path / "hermes"
    (host / "hermes_cli").mkdir(parents=True)
    (host / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    (host / "hermes").write_text("", encoding="utf-8")
    monkeypatch.setattr(qa, "_git", lambda _root, *_args: next(iter(qa.PINS)))
    commit, name = qa.verify_hermes(host)
    assert commit == "c8d4e43efd296f632116cf3f17738aa2c4838460"
    assert name == "main"

    monkeypatch.setattr(qa, "_git", lambda _root, *_args: "f" * 40)
    with pytest.raises(qa.QAError, match="not one of the verified pins"):
        qa.verify_hermes(host)


def _golden_fixture(tmp_path: Path, *, host: str, plugin: str, payload: str) -> Path:
    evidence_dir = tmp_path / "golden"
    evidence_dir.mkdir()
    artifact = evidence_dir / "desktop.png"
    artifact.write_bytes(b"observed desktop screenshot fixture")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    evidence = {
        "desktop_observed": True,
        "surface": "Hermes Desktop",
        "hermes_commit": host,
        "plugin_commit": plugin,
        "plugin_payload_sha256": payload,
        "steps": [
            {"id": step, "ok": True, "artifacts": [{"path": str(artifact), "sha256": digest}]}
            for step in qa.GOLDEN_STEPS
        ],
    }
    path = evidence_dir / "desktop-evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def test_golden_requires_all_steps_with_real_file_hashes(tmp_path):
    evidence = _golden_fixture(tmp_path, host="host", plugin="plugin", payload="payload")
    qa.validate_golden_evidence(
        evidence, hermes_commit="host", plugin_commit="plugin", plugin_hash="payload"
    )
    parsed = json.loads(evidence.read_text(encoding="utf-8"))
    parsed["steps"].pop()
    evidence.write_text(json.dumps(parsed), encoding="utf-8")
    with pytest.raises(qa.QAError, match="journey steps are unverified"):
        qa.validate_golden_evidence(
            evidence, hermes_commit="host", plugin_commit="plugin", plugin_hash="payload"
        )


def test_golden_evidence_must_match_tested_payload_and_artifact_hash(tmp_path):
    evidence = _golden_fixture(tmp_path, host="host", plugin="plugin", payload="payload")
    with pytest.raises(qa.QAError, match="plugin_payload_sha256"):
        qa.validate_golden_evidence(
            evidence, hermes_commit="host", plugin_commit="plugin", plugin_hash="other"
        )
    parsed = json.loads(evidence.read_text(encoding="utf-8"))
    parsed["steps"][0]["artifacts"][0]["sha256"] = "0" * 64
    evidence.write_text(json.dumps(parsed), encoding="utf-8")
    with pytest.raises(qa.QAError, match="journey steps are unverified"):
        qa.validate_golden_evidence(
            evidence, hermes_commit="host", plugin_commit="plugin", plugin_hash="payload"
        )


def test_powershell_literal_quotes_paths_without_shell_concatenation():
    assert qa._ps_literal("C:\\Task's folder\\x") == "'C:\\Task''s folder\\x'"
