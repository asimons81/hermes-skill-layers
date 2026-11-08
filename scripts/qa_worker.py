#!/usr/bin/env python3
"""Worker executed inside Hermes' documented PM environment by scripts/qa.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()), key=lambda p: p.as_posix()
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".skill-layers-export.json"
    }


def payload_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()), key=lambda p: p.as_posix()
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def renamed_identity(content: bytes, old: str, new: str) -> bytes:
    old_line = f"name: {old}".encode()
    new_line = f"name: {new}".encode()
    check(content.count(old_line) == 1, "Synthetic identity was not unique in SKILL.md")
    return content.replace(old_line, new_line, 1)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


WORKFLOW_GUARD: Path | None = None


def call_hermes(
    root: Path, args: list[str], *, guard: Path | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    guard = guard or WORKFLOW_GUARD
    if guard is not None:
        env["PYTHONPATH"] = str(guard)
    result = subprocess.run(
        [sys.executable, str(root / "hermes"), *args],
        cwd=root,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Hermes command failed ({args[0]}): exit={result.returncode}; "
            f"stdout-tail={result.stdout[-1800:]!r}; stderr-tail={result.stderr[-1800:]!r}"
        )
    return result


def json_envelope(root: Path, args: list[str], *, guard: Path | None = None) -> dict[str, Any]:
    result = call_hermes(root, args, guard=guard)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hermes command did not return JSON: {args[0]}") from exc
    if not response.get("ok"):
        raise RuntimeError(f"Skill Layers command failed: {response.get('error')}")
    return response


def json_command(root: Path, args: list[str], *, guard: Path | None = None) -> dict[str, Any]:
    return json_envelope(root, args, guard=guard).get("data", {})


def fresh_host_verification(root: Path) -> dict[str, Any]:
    """Re-enter the documented PM helper after admission so Python sees its new generation."""
    script = textwrap.dedent(
        """
        import json, os, socket
        log_path = os.environ['QA_NETWORK_ATTEMPTS']
        def denied(*_args, **_kwargs):
            with open(log_path, 'a', encoding='utf-8') as stream:
                stream.write('socket\\n')
            raise OSError('Skill Layers QA denied outbound networking')
        socket.socket.connect = denied
        socket.socket.connect_ex = denied
        socket.create_connection = denied
        import hermes_cli.main as host_main
        from skill_layers.plugin import _read_tool
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
        read_result = json.loads(_read_tool('list', {}))
        with TestClient(app) as client:
            path = '/api/plugins/skill-layers/layers'
            accepted = client.get(path, headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
            denied_response = client.get(path)
            wrong_scope = client.post(
                '/api/plugins/skill-layers/checkpoint',
                json={
                    'layer_id': '00000000-0000-4000-8000-000000000000',
                    'expected_profile': 'profile-not-this-one',
                    'request_id': '8bcd2b06-e6f5-4c64-839f-3469514d9559',
                },
                headers={_SESSION_HEADER_NAME: _SESSION_TOKEN},
            )
        spec = __import__('importlib.util').util.find_spec('skill_layers')
        expected_host = os.path.join(os.environ['QA_HERMES_ROOT'], 'hermes_cli', 'main.py')
        host_matches = os.path.realpath(host_main.__file__) == expected_host
        package_origin = spec.origin.replace('\\\\', '/') if spec else ''
        package_is_shared = bool(
            spec and '/plugins/skill-layers/skill_layers/' not in package_origin
        )
        loaded = {
            'host_module_matches_checkout': host_matches,
            'shared_package_loaded': spec is not None,
            'shared_package_outside_plugin_install': package_is_shared,
            'tool_ok': read_result.get('ok') is True,
            'tool_bounded': len(json.dumps(read_result).encode('utf-8')) <= 32768,
            'profile_id': read_result.get('profile_id'),
            'authenticated_status': accepted.status_code,
            'authenticated_ok': accepted.status_code == 200 and accepted.json().get('ok') is True,
            'unauthenticated_status': denied_response.status_code,
            'profile_mismatch_status': wrong_scope.status_code,
            'profile_mismatch_code': wrong_scope.json().get('error', {}).get('code'),
        }
        print(json.dumps(loaded, sort_keys=True))
        """
    )
    env = dict(os.environ)
    command: list[str]
    if os.name != "nt":
        helper = root / "scripts" / "run-in-hermes-env"
        command = [str(helper), "python", "-c", script]
    else:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("Windows QA needs PowerShell for PM environment refresh")
        script_path = Path(os.environ["HERMES_HOME"]) / "qa-fresh-python.ps1"
        activate = root / "activate.ps1"

        def ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        python_code = ps_quote(script)
        ps_script = (
            "$ErrorActionPreference = 'Stop'\n"
            f". {ps_quote(str(activate))}\n"
            "$qaPython = if ($env:PYTHON) { $env:PYTHON } else { 'python' }\n"
            f"& $qaPython -c {python_code}\n"
            "exit $LASTEXITCODE\n"
        )
        script_path.write_text(ps_script, encoding="utf-8")
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
    result = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode:
        raise RuntimeError(
            f"Fresh PM generation probe failed: exit={result.returncode}; "
            f"stdout-tail={result.stdout[-1600:]!r}; stderr-tail={result.stderr[-1600:]!r}"
        )
    report = None
    for line in reversed((result.stdout + "\n" + result.stderr).splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "shared_package_loaded" in candidate:
            report = candidate
            break
    if report is None:
        raise RuntimeError(
            "Fresh PM generation probe omitted JSON evidence; "
            f"stdout-tail={result.stdout[-1200:]!r}; stderr-tail={result.stderr[-1200:]!r}"
        )
    if not report.get("host_module_matches_checkout") or not report.get("shared_package_loaded"):
        raise RuntimeError("Fresh PM Python cannot import the admitted shared package")
    if not report.get("shared_package_outside_plugin_install"):
        raise RuntimeError("Shared package import resolved inside plugin installation directory")
    if not report.get("tool_ok") or not report.get("tool_bounded"):
        raise RuntimeError("Read-only registered tool failed or exceeded its output bound")
    if not report.get("authenticated_ok") or report.get("unauthenticated_status") != 401:
        raise RuntimeError("Real host plugin API authentication probe failed")
    if (
        report.get("profile_mismatch_status") != 409
        or report.get("profile_mismatch_code") != "PROFILE_MISMATCH"
    ):
        raise RuntimeError("Real host API did not reject mismatched profile mutation scope")
    return report


def run() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", required=True, type=Path)
    parser.add_argument("--plugin-dir", required=True, type=Path)
    parser.add_argument("--suite", required=True, choices=("integration", "golden"))
    parser.add_argument("--variant", required=True)
    parser.add_argument("--plugin-commit", required=True)
    parser.add_argument("--plugin-hash", required=True)
    args = parser.parse_args()
    root = args.hermes_root.resolve(strict=True)
    plugin_argument = args.plugin_dir.absolute()
    plugin_dir = plugin_argument.resolve(strict=True)
    home = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
    check(
        plugin_argument == home / "plugins" / "skill-layers",
        "Plugin payload is outside the disposable profile",
    )
    check(
        payload_digest(plugin_dir) == args.plugin_hash,
        "Disposable plugin copy differs from recorded snapshot hash",
    )

    import hermes_cli.main as hermes_main  # type: ignore[import-not-found]

    module_path = Path(hermes_main.__file__).resolve()
    check(
        module_path == root / "hermes_cli" / "main.py",
        "Imported Hermes module is not from --hermes-root",
    )
    rev = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    check(
        rev
        in {"c8d4e43efd296f632116cf3f17738aa2c4838460", "2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19"},
        "Hermes commit is not a supported pin",
    )
    version = call_hermes(root, ["--version"]).stdout.strip()

    # Hermes' documented catalog admission gate builds plugin metadata and admits declared deps.
    validation = call_hermes(
        root, ["plugins", "validate", str(plugin_dir), "--install-deps", "--json"], input_text="y\n"
    )
    validation_data = json.loads(validation.stdout)
    check(validation_data.get("ok") is True, "Hermes plugin validation failed")
    # This is an explicit operator-selected command in a disposable profile; no --yolo is used.
    call_hermes(root, ["plugins", "enable", "skill-layers"], input_text="y\n")
    global WORKFLOW_GUARD
    WORKFLOW_GUARD = Path(os.environ["QA_GUARD_DIR"])
    attempts = Path(os.environ["QA_NETWORK_ATTEMPTS"])

    def deny_network(*_args: Any, **_kwargs: Any) -> Any:
        with attempts.open("a", encoding="utf-8") as stream:
            stream.write("socket\n")
        raise OSError("Skill Layers QA denied outbound networking")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    doctor = json_command(root, ["skill-layers", "doctor", "--json"])
    check(
        doctor.get("cli_requires") == "plugins.isolation: in_process",
        "Host CLI boundary was not surfaced",
    )

    report: dict[str, Any] = {
        "hermes_commit": rev,
        "hermes_module": str(module_path),
        "hermes_version": version,
        "plugin_commit": args.plugin_commit,
        "plugin_payload_sha256": args.plugin_hash,
        "variant": args.variant,
        "checks": [],
    }

    def passed(name: str) -> None:
        report["checks"].append({"id": name, "ok": True})

    passed("loader-validation-cli-doctor")
    if args.variant == "profile-b":
        scope_a = os.environ.get("QA_EXPECT_OTHER_PROFILE")
        listed_envelope = json_envelope(root, ["skill-layers", "list", "--json"])
        listed = listed_envelope["data"]
        check(not listed.get("layers"), "Profile B contains layers created only in profile A")
        sources = json_command(root, ["skill-layers", "sources", "--json"])
        check(
            not any(
                item.get("selector") == "qa-release-notes" for item in sources.get("sources", [])
            ),
            "Profile B discovered the synthetic source from another profile",
        )
        check(
            not attempts.exists() or not attempts.read_text(encoding="utf-8").strip(),
            "Profile B read workflow attempted networking",
        )
        independent = home / "skills" / "qa-profile-b"
        independent.mkdir(parents=True, exist_ok=False)
        independent_skill = independent / "SKILL.md"
        independent_skill.write_text(
            "---\nname: qa-profile-b\ndescription: Independent profile fixture.\n---\n"
            "# Profile B only\n\nThis source belongs to profile B.\n",
            encoding="utf-8",
            newline="",
        )
        independent_before = tree_digest(independent)
        independent_sources = json_command(root, ["skill-layers", "sources", "--json"])["sources"]
        source_matches = [
            item for item in independent_sources if item.get("selector") == "qa-profile-b"
        ]
        check(len(source_matches) == 1, "Profile B did not discover its independent source")
        derived = json_command(
            root,
            ["skill-layers", "derive", source_matches[0]["id"], "--label", "Profile B", "--json"],
        )
        candidate = json_command(root, ["skill-layers", "diff", derived["candidate_id"], "--json"])
        check(candidate.get("state") == "ready", "Profile B derive candidate is not ready")
        independent_publish = json_command(
            root,
            [
                "skill-layers",
                "publish",
                derived["candidate_id"],
                "--revision",
                str(candidate["revision"]),
                "--review-hash",
                candidate["review_hash"],
                "--json",
            ],
        )
        independent_export = independent_publish.get("export", {})
        check(
            Path(independent_export.get("directory", "")).is_dir(),
            "Profile B publication did not create its independent export",
        )
        check(
            tree_digest(independent) == independent_before,
            "Profile B derive publication changed its source",
        )
        check(
            independent_export.get("selector", "").startswith("sl-"),
            "Profile B publication did not create a distinct ordinary skill",
        )
        report["profile_id"] = listed_envelope.get("profile_id")
        report["checks"].append(
            {"id": "profile-isolation", "ok": True, "other_profile_marker": scope_a}
        )
        report["checks"].append(
            {
                "id": "profile-b-independent-derive-publish-source-preserved",
                "ok": True,
                "selector": independent_export["selector"],
            }
        )
        return report

    if args.variant != "profile-a":
        raise RuntimeError("Unexpected disposable profile variant")
    source = home / "skills" / "qa-release-notes"
    source.mkdir(parents=True, exist_ok=False)
    source_md = source / "SKILL.md"
    source_md.write_text(
        "---\nname: qa-release-notes\ndescription: Synthetic integration fixture.\n---\n"
        "# Release notes\n\nBase instructions.\n\n## Review\n\nKeep the release summary concise.\n",
        encoding="utf-8",
        newline="",
    )
    (source / "references").mkdir()
    (source / "references" / "format.md").write_text(
        "Synthetic support reference v1.\n", encoding="utf-8"
    )
    source_before = tree_digest(source)

    sources = json_command(root, ["skill-layers", "sources", "--json"])["sources"]
    chosen = [item for item in sources if item.get("selector") == "qa-release-notes"]
    check(len(chosen) == 1, "Synthetic source was not discovered exactly once")
    source_id = chosen[0]["id"]
    derive = json_command(
        root, ["skill-layers", "derive", source_id, "--label", "Host QA", "--json"]
    )
    layer_id, candidate_id = derive["layer_id"], derive["candidate_id"]
    candidate = json_command(root, ["skill-layers", "diff", candidate_id, "--json"])
    check(
        candidate["state"] == "ready" and candidate["review_hash"],
        "Derive candidate is not reviewed/ready",
    )
    check(tree_digest(source) == source_before, "Derive changed source bytes")
    published = json_command(
        root,
        [
            "skill-layers",
            "publish",
            candidate_id,
            "--revision",
            str(candidate["revision"]),
            "--review-hash",
            candidate["review_hash"],
            "--json",
        ],
    )
    export = published["export"]
    selector, export_dir = export["selector"], Path(export["directory"])
    check(
        selector.startswith("sl-") and export_dir.is_dir(),
        "Published derivative is not a new ordinary skill",
    )
    check(tree_digest(source) == source_before, "Derive publication changed source bytes")
    expected_source_support = (source / "references" / "format.md").read_bytes()
    expected_derive_tree = {
        "SKILL.md": renamed_identity(source_md.read_bytes(), "qa-release-notes", selector),
        "references/format.md": expected_source_support,
    }
    check(
        tree_bytes(export_dir) == expected_derive_tree,
        "Derive output tree differs from the independent expected tree",
    )
    passed("derive-publish-source-preserved-support-files")

    from tools.skill_manager_tool import skill_manage  # type: ignore[import-not-found]
    from tools.skills_tool import find_all_skills, skill_view

    found = [
        item
        for item in find_all_skills()
        if item.get("name") == selector or item.get("load_name") == selector
    ]
    check(bool(found), "Hermes native skill discovery did not find published selector")
    viewed = json.loads(skill_view(selector))
    check(viewed.get("success") is True, "Hermes native skill_view rejected published selector")
    edit = json.loads(
        skill_manage(
            action="patch",
            name=selector,
            old_string="Base instructions.",
            new_string="Base instructions plus the local migration notes.",
        )
    )
    check(
        edit.get("success") is True and not edit.get("staged"),
        f"Native skill edit did not apply: {edit}",
    )
    check(
        "local migration notes" in (export_dir / "SKILL.md").read_text(encoding="utf-8"),
        "Native edit is absent from derivative",
    )
    check(tree_digest(source) == source_before, "Native edit changed source bytes")
    checkpoint_expected = tree_bytes(export_dir)
    immutable_exports: dict[str, dict[str, bytes]] = {selector: checkpoint_expected}
    passed("native-discover-view-edit")

    checkpoint = json_command(root, ["skill-layers", "checkpoint", layer_id, "--json"])[
        "checkpoint"
    ]
    checkpoint_id = checkpoint["id"]
    check(tree_digest(source) == source_before, "Checkpoint changed source bytes")
    source_md.write_text(
        "---\nname: qa-release-notes\ndescription: Synthetic integration fixture.\n---\n"
        "# Release notes\n\nBase instructions.\n\n## Review\n\n"
        "Keep the release summary concise and include issue links.\n",
        encoding="utf-8",
        newline="",
    )
    source_v2 = tree_digest(source)
    refresh = json_command(root, ["skill-layers", "refresh", layer_id, "--json"])
    refresh_diff = json_command(root, ["skill-layers", "diff", refresh["candidate_id"], "--json"])
    check(
        refresh_diff["state"] == "ready",
        "Independent local/source update became an unresolved conflict",
    )
    check(tree_digest(source) == source_v2, "Refresh changed source bytes")
    refresh_publish = json_command(
        root,
        [
            "skill-layers",
            "publish",
            refresh["candidate_id"],
            "--revision",
            str(refresh_diff["revision"]),
            "--review-hash",
            refresh_diff["review_hash"],
            "--json",
        ],
    )
    merged_dir = Path(refresh_publish["export"]["directory"])
    merged_text = (merged_dir / "SKILL.md").read_text(encoding="utf-8")
    check(
        "local migration notes" in merged_text and "include issue links" in merged_text,
        "Refresh lost independent local or source change",
    )
    check(tree_digest(source) == source_v2, "Refresh publication changed source bytes")
    expected_merged = source_md.read_bytes().replace(
        b"Base instructions.", b"Base instructions plus the local migration notes.", 1
    )
    expected_merged = renamed_identity(
        expected_merged, "qa-release-notes", refresh_publish["export"]["selector"]
    )
    expected_refresh_tree = {
        "SKILL.md": expected_merged,
        "references/format.md": expected_source_support,
    }
    check(
        tree_bytes(merged_dir) == expected_refresh_tree,
        "Refresh output differs from the independent expected tree",
    )
    check(
        tree_bytes(export_dir) == immutable_exports[selector], "Refresh modified the prior export"
    )
    immutable_exports[refresh_publish["export"]["selector"]] = expected_refresh_tree
    latest_status = json_command(root, ["skill-layers", "status", layer_id, "--json"])
    refreshed_export = next(
        item
        for item in latest_status["exports"]
        if item["selector"] == refresh_publish["export"]["selector"]
    )
    check(
        refreshed_export["base_snapshot"] == refresh_diff["captured"]["source"],
        "Refresh export did not associate the new source U as its next base",
    )
    passed("refresh-three-way-independent-edits")

    before_exports = sorted(
        path.name
        for path in (home / "skills").iterdir()
        if path.is_dir() and path.name.startswith("sl-")
    )
    merged_text = (merged_dir / "SKILL.md").read_text(encoding="utf-8")
    changed_local = merged_text.replace("# Release notes", "# Local release heading", 1)
    check(changed_local != merged_text, "Fixture heading not found")
    (merged_dir / "SKILL.md").write_text(changed_local, encoding="utf-8", newline="")
    immutable_exports[refresh_publish["export"]["selector"]] = tree_bytes(merged_dir)
    source_md.write_text(
        source_md.read_text(encoding="utf-8").replace(
            "# Release notes", "# Source release heading", 1
        ),
        encoding="utf-8",
        newline="",
    )
    source_conflict = tree_digest(source)
    conflict = json_command(root, ["skill-layers", "refresh", layer_id, "--json"])
    conflict_diff = json_command(root, ["skill-layers", "diff", conflict["candidate_id"], "--json"])
    check(
        conflict_diff["state"] == "needs_resolution" and conflict_diff["unresolved"],
        "Same-line edit did not produce recorded conflict",
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(root / "hermes"),
            "skill-layers",
            "publish",
            conflict["candidate_id"],
            "--revision",
            str(conflict_diff["revision"]),
            "--review-hash",
            conflict_diff["review_hash"],
            "--json",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(WORKFLOW_GUARD)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    check(
        rejected.returncode != 0 and "CONFLICTS_UNRESOLVED" in rejected.stdout,
        "Unresolved candidate published or had an unexpected failure",
    )
    check(
        tree_digest(source) == source_conflict, "Conflict preview or refusal changed source bytes"
    )
    conflict_path = conflict_diff["unresolved"][0]
    json_command(
        root,
        [
            "skill-layers",
            "resolve",
            conflict["candidate_id"],
            "--revision",
            str(conflict_diff["revision"]),
            "--path",
            conflict_path,
            "--take",
            "local",
            "--json",
        ],
    )
    reviewed = json_command(root, ["skill-layers", "diff", conflict["candidate_id"], "--json"])
    check(
        reviewed["state"] == "ready" and not reviewed["unresolved"],
        "Explicit resolution did not clear conflict",
    )
    conflict_publish = json_command(
        root,
        [
            "skill-layers",
            "publish",
            conflict["candidate_id"],
            "--revision",
            str(reviewed["revision"]),
            "--review-hash",
            reviewed["review_hash"],
            "--json",
        ],
    )
    resolved_dir = Path(conflict_publish["export"]["directory"])
    expected_conflict = renamed_identity(
        changed_local.encode("utf-8"),
        refresh_publish["export"]["selector"],
        conflict_publish["export"]["selector"],
    )
    check(
        tree_bytes(resolved_dir)
        == {"SKILL.md": expected_conflict, "references/format.md": expected_source_support},
        "Resolved conflict output differs from the independent expected tree",
    )
    check(
        all(
            tree_bytes(home / "skills" / name) == files for name, files in immutable_exports.items()
        ),
        "Conflict publication modified a prior export",
    )
    check(
        "Local release heading" in (resolved_dir / "SKILL.md").read_text(encoding="utf-8"),
        "Resolved local file choice not retained",
    )
    check(
        tree_digest(source) == source_conflict,
        "Conflict resolution/publication changed source bytes",
    )
    check(
        all((home / "skills" / name).is_dir() for name in before_exports),
        "Refresh/conflict overwrote a previous export",
    )
    immutable_exports[conflict_publish["export"]["selector"]] = tree_bytes(resolved_dir)
    passed("conflict-block-explicit-resolution-new-export")

    rollback = json_command(
        root, ["skill-layers", "rollback", layer_id, "--checkpoint", checkpoint_id, "--json"]
    )
    rollback_diff = json_command(root, ["skill-layers", "diff", rollback["candidate_id"], "--json"])
    check(rollback_diff["state"] == "ready", "Checkpoint rollback candidate is not ready")
    rollback_result = json_command(
        root,
        [
            "skill-layers",
            "publish",
            rollback["candidate_id"],
            "--revision",
            str(rollback_diff["revision"]),
            "--review-hash",
            rollback_diff["review_hash"],
            "--json",
        ],
    )
    rollback_dir = Path(rollback_result["export"]["directory"])
    expected_rollback = dict(checkpoint_expected)
    expected_rollback["SKILL.md"] = renamed_identity(
        expected_rollback["SKILL.md"], selector, rollback_result["export"]["selector"]
    )
    check(
        tree_bytes(rollback_dir) == expected_rollback,
        "Rollback differs from checkpoint expected contents",
    )
    check(
        all(
            tree_bytes(home / "skills" / name) == files for name, files in immutable_exports.items()
        ),
        "Rollback modified an earlier export",
    )
    rollback_status = json_command(root, ["skill-layers", "status", layer_id, "--json"])
    rollback_export = next(
        item
        for item in rollback_status["exports"]
        if item["selector"] == rollback_result["export"]["selector"]
    )
    check(
        rollback_export["base_snapshot"] == checkpoint["base_snapshot"],
        "Rollback did not carry the checkpoint's associated source base",
    )
    check(
        all((home / "skills" / name).is_dir() for name in before_exports),
        "Rollback removed a previous export",
    )
    check(tree_digest(source) == source_conflict, "Rollback changed source bytes")
    passed("rollback-fresh-identity-checkpoint-preserved-prior-exports")

    # Re-enter Hermes through the documented helper after PM admission. The long-lived
    # worker retains the previous generation, so this child verifies shared imports and
    # backend auth without modifying sys.path.
    plugin_data = home / "plugin-data" / "skill-layers"
    before_read = tree_digest(plugin_data)
    skills_before_read = tree_digest(home / "skills")
    host_probe = fresh_host_verification(root)
    after_read = tree_digest(plugin_data)
    skills_after_read = tree_digest(home / "skills")
    check(before_read == after_read, "Read-only agent tool mutated plugin-owned files")
    check(skills_before_read == skills_after_read, "Read-only agent tool mutated profile skills")
    passed("readonly-bounded-agent-tool-no-filesystem-mutation")
    check(host_probe.get("profile_id"), "Read-only tool omitted profile identity")
    passed("real-host-api-auth-profile-scope")

    # T17 uses only controlled synthetic skills and the disposable profile. Lock refusal
    # and write approval are independent host controls; neither is rerouted to publication.
    lock_target = source.name
    call_hermes(root, ["curator", "lock", lock_target])
    source_locked_before = tree_digest(source)
    locked = json.loads(
        skill_manage(
            action="patch",
            name=lock_target,
            old_string="Base instructions.",
            new_string="must not apply",
        )
    )
    check(
        locked.get("success") is False and locked.get("_locked") is True,
        "Native lock refusal was not honored",
    )
    check(tree_digest(source) == source_locked_before, "Locked source bytes changed")
    call_hermes(root, ["curator", "unlock", lock_target])
    passed("native-lock-refusal-no-rerouting")

    # Native approval is another independent fixture: refusal/rejection must leave that
    # skill's bytes intact, while a separately reviewed operator publication stays explicit.
    approval_selector = rollback_result["export"]["selector"]
    approval_md = Path(rollback_result["export"]["directory"]) / "SKILL.md"
    approval_before = approval_md.read_bytes()
    call_hermes(root, ["config", "set", "skills.write_approval", "true"])
    try:
        staged = json.loads(
            skill_manage(
                action="patch",
                name=approval_selector,
                old_string="local migration notes",
                new_string="approval should stage without editing",
            )
        )
        check(
            staged.get("success") is True and staged.get("staged") is True,
            "Native approval did not stage the edit",
        )
        pending_id = staged.get("pending_id")
        check(
            isinstance(pending_id, str) and pending_id,
            "Native approval staging omitted its pending ID",
        )
        check(
            approval_md.read_bytes() == approval_before,
            "Approval-required native edit changed skill bytes",
        )
        from hermes_cli.write_approval_commands import handle_pending_subcommand  # type: ignore[import-not-found]
        from tools import write_approval as write_approval_module  # type: ignore[import-not-found]

        rejected_text = handle_pending_subcommand(
            write_approval_module.SKILLS, ["reject", pending_id]
        )
        check(
            "Rejected pending skills write" in rejected_text,
            "Native approval rejection was not recorded",
        )
        check(
            write_approval_module.get_pending(write_approval_module.SKILLS, pending_id) is None,
            "Rejected native edit remains pending",
        )
        check(
            approval_md.read_bytes() == approval_before, "Rejected native edit changed skill bytes"
        )

        boundary = json_command(root, ["skill-layers", "doctor", "--json"])
        check(
            boundary.get("native_publication_queue") is False,
            "Plugin misstated native approval queue ownership",
        )
        check(
            "does not enter Hermes' native" in boundary.get("publication_boundary", ""),
            "Plugin publication boundary is not disclosed",
        )

        second_source = home / "skills" / "qa-policy-boundary"
        second_source.mkdir(exist_ok=False)
        (second_source / "SKILL.md").write_text(
            "---\nname: qa-policy-boundary\ndescription: Synthetic policy boundary fixture.\n---\n"
            "# Policy boundary\n\nExplicit operator publication fixture.\n",
            encoding="utf-8",
            newline="",
        )
        second_source_before = tree_digest(second_source)
        second_sources = json_command(root, ["skill-layers", "sources", "--json"])["sources"]
        second_id = next(
            item["id"] for item in second_sources if item["selector"] == "qa-policy-boundary"
        )
        policy_derive = json_command(
            root, ["skill-layers", "derive", second_id, "--label", "Policy QA", "--json"]
        )
        policy_diff = json_command(
            root, ["skill-layers", "diff", policy_derive["candidate_id"], "--json"]
        )
        policy_publish = json_command(
            root,
            [
                "skill-layers",
                "publish",
                policy_derive["candidate_id"],
                "--revision",
                str(policy_diff["revision"]),
                "--review-hash",
                policy_diff["review_hash"],
                "--json",
            ],
        )
        check(
            Path(policy_publish["export"]["directory"]).is_dir(),
            "Explicit operator publication did not publish its new skill",
        )
        check(
            tree_digest(second_source) == second_source_before,
            "Separate publication changed its source",
        )
        check(
            write_approval_module.get_pending(write_approval_module.SKILLS, pending_id) is None,
            "Operator publication rerouted a rejected native edit",
        )
        passed("native-approval-staged-rejected-explicit-publication-boundary")
    finally:
        call_hermes(root, ["config", "set", "skills.write_approval", "false"])

    # Offline instrumentation applies to plugin-owned Python child workflows. Network
    # activity is made to fail closed; there is no provider/model request in these steps.
    check(
        not attempts.exists() or not attempts.read_text(encoding="utf-8").strip(),
        "A plugin workflow attempted outbound networking",
    )
    passed("plugin-owned-workflow-outbound-network-denied")

    # Exercise the host-managed installed-plugin lifecycle only after all plugin-owned checks.
    # The export is an ordinary profile skill and must remain usable after disable and removal.
    export_bytes = tree_bytes(export_dir)
    lifecycle_source_before = tree_digest(source)
    plugin_install = home / "plugins" / "skill-layers"
    disable_output = call_hermes(root, ["plugins", "disable", "skill-layers"])
    check(
        "disabled" in (disable_output.stdout + disable_output.stderr).lower()
        or "disable" in (disable_output.stdout + disable_output.stderr).lower(),
        "Hermes did not acknowledge plugin disable",
    )
    disabled_view = json.loads(skill_view(selector))
    check(disabled_view.get("success") is True, "Native view failed while plugin was disabled")
    check(tree_bytes(export_dir) == export_bytes, "Disable changed the published export")
    check(
        tree_digest(source) == lifecycle_source_before,
        "Disable changed the source bytes present at the lifecycle boundary",
    )
    passed("host-disable-native-view-and-export-preserved")

    call_hermes(root, ["plugins", "enable", "skill-layers"], input_text="y\n")
    reenabled_view = json.loads(skill_view(selector))
    check(reenabled_view.get("success") is True, "Native view failed after plugin re-enable")
    check(tree_bytes(export_dir) == export_bytes, "Re-enable changed the published export")
    persisted = json_command(root, ["skill-layers", "list", "--json"])
    check(
        any(item.get("id") == layer_id for item in persisted.get("layers", [])),
        "Plugin layer history did not persist across disable/re-enable",
    )
    check(
        tree_digest(source) == lifecycle_source_before,
        "Re-enable changed the source bytes present at the lifecycle boundary",
    )
    passed("host-reenable-layer-history-persisted")

    call_hermes(root, ["plugins", "remove", "skill-layers"])
    check(
        not plugin_install.exists() and not plugin_install.is_symlink(),
        "Public plugin removal left the plugin installed",
    )
    removed_view = json.loads(skill_view(selector))
    check(removed_view.get("success") is True, "Native view failed after plugin removal")
    check(tree_bytes(export_dir) == export_bytes, "Plugin removal changed the published export")
    check(
        tree_digest(source) == lifecycle_source_before,
        "Plugin removal changed the source bytes present at the lifecycle boundary",
    )
    passed("host-remove-native-view-and-export-preserved")

    report["profile_id"] = host_probe["profile_id"]
    report["source_initial_sha256"] = source_before
    report["source_final_sha256"] = tree_digest(source)
    report["published_selectors"] = [
        export["selector"],
        refresh_publish["export"]["selector"],
        conflict_publish["export"]["selector"],
        rollback_result["export"]["selector"],
    ]
    return report


def main() -> int:
    try:
        result = run()
        report_path = Path(os.environ["QA_REPORT_PATH"])
        report_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    except Exception as exc:
        print(f"integration worker failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
