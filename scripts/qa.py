#!/usr/bin/env python3
"""Run Skill Layers against a pinned, real Hermes checkout in disposable profiles.

Plugin dependency admission can install declared packages. The suite command is an
explicit operator action, and its host changes remain inside newly created disposable roots.
The runner never enables Hermes' YOLO mode and never inspects/logs credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "c8d4e43efd296f632116cf3f17738aa2c4838460": "main",
    "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19": "release-v0.23.0",
}
PAYLOAD_FILES = ("plugin.yaml", "__init__.py", "pyproject.toml", "LICENSE")
PAYLOAD_DIRS = ("skill_layers", "dashboard", "desktop")
GOLDEN_STEPS = (
    "source_create",
    "derive_review",
    "publish",
    "native_view",
    "native_edit",
    "checkpoint",
    "source_v2",
    "refresh_review",
    "refresh_publish",
    "conflict_block",
    "explicit_resolution",
    "conflict_publish",
    "rollback_preview",
    "rollback_publish",
    "old_exports_preserved",
    "source_hashes_verified",
)


class QAError(RuntimeError):
    """A required QA precondition or check failed."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise QAError(f"git {' '.join(args)} failed in the requested Hermes checkout")
    return result.stdout.strip()


def verify_hermes(root: Path) -> tuple[str, str]:
    if not root.is_absolute() or not root.is_dir():
        raise QAError("--hermes-root must be an absolute directory")
    commit = _git(root, "rev-parse", "HEAD")
    pin_name = PINS.get(commit)
    if not pin_name:
        raise QAError(
            "Hermes checkout is not one of the verified pins: expected main c8d4e43... "
            "or released v0.23.0 2bd378e..."
        )
    module = root / "hermes_cli" / "main.py"
    entry = root / "hermes"
    if not module.is_file() or not entry.is_file():
        raise QAError("Requested Hermes checkout is missing hermes_cli/main.py or hermes")
    return commit, pin_name


def payload_digest(plugin_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (p for p in plugin_dir.rglob("*") if p.is_file()), key=lambda p: p.as_posix()
    ):
        relative = path.relative_to(plugin_dir).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def copy_finite_payload(destination: Path) -> None:
    """Copy only shipped runtime files; PM snapshots do not exclude project work/."""
    destination.mkdir(parents=True, exist_ok=False)
    for name in PAYLOAD_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for name in PAYLOAD_DIRS:
        source = ROOT / name
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(source)
            target = destination / name / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    required = ("plugin.yaml", "__init__.py", "pyproject.toml", "skill_layers/__init__.py")
    missing = [name for name in required if not (destination / name).is_file()]
    if missing:
        raise QAError(f"Finite plugin payload is incomplete: {', '.join(missing)}")
    for forbidden in ("work", ".git", ".venv", "node_modules"):
        if (destination / forbidden).exists():
            raise QAError("Finite payload unexpectedly contains project state")


def clean_environment(profile: Path, os_home: Path, runtime: Path) -> dict[str, str]:
    """Return a child-only env mapping; never mutate this process environment."""
    env = dict(os.environ)
    if not profile.is_dir():
        raise QAError("Disposable profile was not created through Hermes' profile command")
    os_home.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    env["HERMES_HOME"] = str(profile)
    env["HERMES_RUNTIME_DIR"] = str(runtime)
    env["HOME"] = str(os_home)
    env["USERPROFILE"] = str(os_home)
    env["APPDATA"] = str(os_home / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(os_home / "AppData" / "Local")
    env["XDG_CONFIG_HOME"] = str(os_home / ".config")
    env["XDG_CACHE_HOME"] = str(os_home / ".cache")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("__HERMES_ACTIVATED", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NO_COLOR"] = "1"
    return env


def create_named_profiles(
    *, hermes_root: Path, run_root: Path, names: tuple[str, ...]
) -> tuple[Path, Path, Path]:
    """Create real named profiles under one isolated Hermes root using its public CLI."""
    hermes_home = run_root / "hermes-home"
    os_home = run_root / "os-home"
    runtime = run_root / "runtime"
    for directory in (hermes_home, os_home, runtime):
        directory.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_RUNTIME_DIR": str(runtime),
            "HOME": str(os_home),
            "USERPROFILE": str(os_home),
            "APPDATA": str(os_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(os_home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(os_home / ".config"),
            "XDG_CACHE_HOME": str(os_home / ".cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
    )
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__HERMES_ACTIVATED"):
        env.pop(key, None)
    for name in names:
        command = [
            "python",
            "-m",
            "hermes_cli.main",
            "profile",
            "create",
            name,
            "--no-alias",
            "--no-skills",
        ]
        psfile = run_root / f"create-{name}.ps1" if os.name == "nt" else None
        result = subprocess.run(
            host_command(hermes_root, command, windows_script=psfile),
            cwd=str(hermes_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            tail = _redact(result.stdout + "\n" + result.stderr, [hermes_home, os_home, runtime])
            raise QAError(f"Hermes public profile create failed for {name}: {tail[-1800:]}")
        created = hermes_home / "profiles" / name
        if not created.is_dir() or not any(
            (created / marker).is_file()
            for marker in (
                "config.yaml",
                ".env",
                "SOUL.md",
                "profile.yaml",
                "auth.json",
                "state.db",
            )
        ):
            raise QAError(f"Hermes profile create omitted the expected live identity for {name}")
    return hermes_home, os_home, runtime


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def host_command(
    root: Path, command: list[str], *, windows_script: Path | None = None
) -> list[str]:
    if os.name != "nt":
        helper = root / "scripts" / "run-in-hermes-env"
        if not helper.is_file():
            raise QAError("Hermes POSIX scripts/run-in-hermes-env is missing")
        return [str(helper), *command]
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    activate = root / "activate.ps1"
    if not powershell or not activate.is_file() or windows_script is None:
        raise QAError("Windows requires PowerShell and the requested Hermes activate.ps1")
    executable = "$env:PYTHON" if command[0] == "python" else _ps_literal(command[0])
    if command[0] == "python":
        executable = "if ($env:PYTHON) { $env:PYTHON } else { 'python' }"
    quoted = " ".join(_ps_literal(arg) for arg in command[1:])
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f". {_ps_literal(str(activate))}\n"
        f"$qaPython = {executable}\n"
        f"& $qaPython {quoted}\n"
        "exit $LASTEXITCODE\n"
    )
    windows_script.write_text(script, encoding="utf-8")
    return [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(windows_script),
    ]


def _redact(text: str, roots: list[Path]) -> str:
    value = text
    for root in roots:
        value = value.replace(str(root), "<disposable-root>")
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", value)
    value = re.sub(
        r"(?i)(api[_-]?key|token|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2<redacted>", value
    )
    return value


def run_profile(
    *,
    hermes_root: Path,
    commit: str,
    suite: str,
    run_root: Path,
    variant: str,
    plugin_snapshot: Path,
    plugin_commit: str,
    plugin_hash: str,
) -> dict[str, Any]:
    profile = run_root / "hermes-home" / "profiles" / variant
    os_home = run_root / "os-home"
    runtime = run_root / "runtime"
    env = clean_environment(profile, os_home, runtime)
    # Reuse one finite disposable payload tree; never link the development project root.
    target = profile / "plugins" / "skill-layers"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(plugin_snapshot, target_is_directory=True)
    guard = run_root / "offline-guard"
    guard.mkdir(parents=True, exist_ok=True)
    attempts = run_root / f"{variant}-network-attempts.txt"
    (guard / "sitecustomize.py").write_text(
        "import os, socket\n"
        "_log = os.environ.get('QA_NETWORK_ATTEMPTS')\n"
        "def _blocked(*args, **kwargs):\n"
        "    if _log:\n"
        "        with open(_log, 'a', encoding='utf-8') as stream: stream.write('socket\\n')\n"
        "    raise OSError('Skill Layers QA denied outbound networking')\n"
        "socket.socket.connect = _blocked\n"
        "socket.socket.connect_ex = _blocked\n"
        "socket.create_connection = _blocked\n",
        encoding="utf-8",
    )
    env["QA_GUARD_DIR"] = str(guard)
    env["QA_NETWORK_ATTEMPTS"] = str(attempts)
    env["QA_REPORT_PATH"] = str(run_root / f"{variant}-report.json")
    env["QA_HERMES_ROOT"] = str(hermes_root)
    env["QA_EXPECT_OTHER_PROFILE"] = "a-separate-disposable-profile"
    worker = ROOT / "scripts" / "qa_worker.py"
    cmd = [
        "python",
        str(worker),
        "--hermes-root",
        str(hermes_root),
        "--plugin-dir",
        str(target),
        "--suite",
        suite,
        "--variant",
        variant,
        "--plugin-commit",
        plugin_commit,
        "--plugin-hash",
        plugin_hash,
    ]
    psfile = run_root / f"activate-{variant}.ps1" if os.name == "nt" else None
    invocation = host_command(hermes_root, cmd, windows_script=psfile)
    env_before = dict(os.environ)
    result = subprocess.run(
        invocation, cwd=str(hermes_root), env=env, capture_output=True, text=True, timeout=2400
    )
    if dict(os.environ) != env_before:
        raise QAError("Runner altered its parent process environment")
    roots = [profile, os_home, runtime, run_root]
    stdout = _redact(result.stdout, roots)
    stderr = _redact(result.stderr, roots)
    log = run_root / f"{variant}.log"
    log.write_text("[stdout]\n" + stdout + "\n[stderr]\n" + stderr, encoding="utf-8")
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"{variant}: host integration {status} (exit {result.returncode});", f"log={log}")
    if result.returncode:
        tail = "\n".join((stdout + "\n" + stderr).splitlines()[-24:])
        print(tail, file=sys.stderr)
        raise QAError(f"Hermes integration worker failed for {variant}")
    report_path = run_root / f"{variant}-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QAError("Integration worker omitted its result report") from exc
    if report.get("hermes_commit") != commit:
        raise QAError("Worker reported a different Hermes commit")
    if (
        report.get("plugin_commit") != plugin_commit
        or report.get("plugin_payload_sha256") != plugin_hash
    ):
        raise QAError("Worker report does not match the copied plugin payload")
    return report


def validate_golden_evidence(
    path: Path, *, hermes_commit: str, plugin_commit: str, plugin_hash: str
) -> None:
    if not path.is_file():
        raise QAError(f"Required real Desktop evidence is unexecuted: {path}")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QAError("Desktop evidence is not valid JSON") from exc
    if evidence.get("desktop_observed") is not True:
        raise QAError("Desktop evidence must explicitly record an observed real Desktop journey")
    if evidence.get("surface") != "Hermes Desktop":
        raise QAError("Golden evidence must identify the observed Hermes Desktop surface")
    for key, expected in (
        ("hermes_commit", hermes_commit),
        ("plugin_commit", plugin_commit),
        ("plugin_payload_sha256", plugin_hash),
    ):
        if evidence.get(key) != expected:
            raise QAError(f"Desktop evidence {key} does not match this tested build")
    steps = evidence.get("steps")
    if not isinstance(steps, list):
        raise QAError("Desktop evidence must include a step list")
    observed: set[str] = set()
    for item in steps:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        step_id = item.get("id")
        artifacts = item.get("artifacts")
        if not isinstance(step_id, str) or not isinstance(artifacts, list) or not artifacts:
            continue
        valid_artifacts = True
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                valid_artifacts = False
                break
            artifact_path = Path(artifact["path"])
            if (
                not artifact_path.is_absolute()
                or not artifact_path.is_file()
                or not _is_within(artifact_path, path.parent)
            ):
                valid_artifacts = False
                break
            actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if artifact.get("sha256") != actual:
                valid_artifacts = False
                break
        if valid_artifacts:
            observed.add(step_id)
    missing = sorted(set(GOLDEN_STEPS) - observed)
    if missing:
        raise QAError("Required real Desktop journey steps are unverified: " + ", ".join(missing))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", required=True, type=Path)
    parser.add_argument("--suite", required=True, choices=("integration", "golden"))
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        hermes_root = args.hermes_root.resolve(strict=True)
        artifacts = args.artifacts
        if not artifacts.is_absolute():
            raise QAError("--artifacts must be absolute")
        artifacts.mkdir(parents=True, exist_ok=True)
        if _is_within(artifacts, hermes_root) or _is_within(hermes_root, artifacts):
            raise QAError("Artifacts must be outside the Hermes checkout")
        if _is_within(ROOT, artifacts):
            raise QAError("Artifacts cannot contain the plugin checkout")
        if _is_within(artifacts, ROOT) and not _is_within(artifacts, ROOT / "work"):
            raise QAError("Artifacts inside the plugin checkout must be under its work/ directory")
        commit, pin_name = verify_hermes(hermes_root)
        if ROOT == hermes_root or _is_within(ROOT, hermes_root):
            raise QAError("The plugin checkout cannot be inside the requested Hermes checkout")
        if _is_within(hermes_root, ROOT) and not _is_within(hermes_root, ROOT / "work"):
            raise QAError(
                "A nested Hermes checkout is permitted only under this project's work/ directory"
            )
        plugin_commit = _git(ROOT, "rev-parse", "HEAD")
        plugin_status = _git(ROOT, "status", "--porcelain=v1")
        plugin_status_hash = hashlib.sha256(plugin_status.encode("utf-8")).hexdigest()
        run_root = artifacts / args.suite / f"{pin_name}-{uuid.uuid4().hex[:12]}"
        run_root.mkdir(parents=True, exist_ok=False)
        # Snapshot once, then copy the same finite tree to every disposable profile.
        plugin_snapshot = run_root / "plugin-snapshot"
        copy_finite_payload(plugin_snapshot)
        plugin_hash = payload_digest(plugin_snapshot)
        snapshot_record = {
            "plugin_commit": plugin_commit,
            "plugin_worktree_dirty": bool(plugin_status),
            "plugin_status_sha256": plugin_status_hash,
            "plugin_payload_sha256": plugin_hash,
            "files": sorted(
                path.relative_to(plugin_snapshot).as_posix()
                for path in plugin_snapshot.rglob("*")
                if path.is_file()
            ),
        }
        (run_root / "plugin-snapshot.json").write_text(
            json.dumps(snapshot_record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Hermes: {pin_name} {commit}; platform={platform.platform()}")
        print(f"Plugin: commit={plugin_commit}; finite-payload-sha256={plugin_hash}")
        create_named_profiles(
            hermes_root=hermes_root,
            run_root=run_root,
            names=("profile-a", "profile-b"),
        )
        reports = []
        reports.append(
            run_profile(
                hermes_root=hermes_root,
                commit=commit,
                suite=args.suite,
                run_root=run_root,
                variant="profile-a",
                plugin_snapshot=plugin_snapshot,
                plugin_commit=plugin_commit,
                plugin_hash=plugin_hash,
            )
        )
        reports.append(
            run_profile(
                hermes_root=hermes_root,
                commit=commit,
                suite=args.suite,
                run_root=run_root,
                variant="profile-b",
                plugin_snapshot=plugin_snapshot,
                plugin_commit=plugin_commit,
                plugin_hash=plugin_hash,
            )
        )
        profile_ids = {report.get("profile_id") for report in reports}
        if None in profile_ids or len(profile_ids) != 2:
            raise QAError("Disposable Hermes profiles did not produce distinct profile scopes")
        if args.suite == "golden":
            evidence = artifacts / "golden" / "desktop-evidence.json"
            validate_golden_evidence(
                evidence, hermes_commit=commit, plugin_commit=plugin_commit, plugin_hash=plugin_hash
            )
            print(f"Real Desktop journey evidence: PASS ({evidence})")
        combined = {
            "suite": args.suite,
            "hermes_commit": commit,
            "hermes_pin": pin_name,
            "platform": platform.platform(),
            "plugin_commit": plugin_commit,
            "plugin_worktree_dirty": bool(plugin_status),
            "plugin_status_sha256": plugin_status_hash,
            "plugin_payload_sha256": plugin_hash,
            "profiles": reports,
            "desktop_evidence": str(artifacts / "golden" / "desktop-evidence.json")
            if args.suite == "golden"
            else None,
            "result": "passed",
        }
        out = run_root / "qa-report.json"
        out.write_text(json.dumps(combined, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{args.suite}: PASS; report={out}")
        return 0
    except (QAError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"QA BLOCKED/FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
