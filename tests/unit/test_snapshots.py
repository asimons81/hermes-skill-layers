import hashlib
import json
import os
from pathlib import Path

import pytest

from skill_layers import windows_capture
from skill_layers.contracts import LayerError, TreeFile
from skill_layers.snapshots import (
    capture_tree,
    make_snapshot,
    validate_path,
    validate_tree,
    verify_snapshot,
    write_tree,
)


def test_manifest_is_exact_canonical_tree_identity():
    tree = {
        "SKILL.md": TreeFile(b"---\nname: demo\n---\nbody\n"),
        "scripts/run.py": TreeFile(b"print('ok')\n", True, 0o100),
    }
    snapshot = make_snapshot(tree, "raw", {"name": "demo"})
    expected_manifest = {
        "format_version": 1,
        "representation": "raw",
        "identity": {"name": "demo"},
        "files": [
            {
                "path": "SKILL.md",
                "sha256": hashlib.sha256(tree["SKILL.md"].content).hexdigest(),
                "size": len(tree["SKILL.md"].content),
                "executable": False,
                "execute_bits": 0,
            },
            {
                "path": "scripts/run.py",
                "sha256": hashlib.sha256(tree["scripts/run.py"].content).hexdigest(),
                "size": len(tree["scripts/run.py"].content),
                "executable": True,
                "execute_bits": 0o100,
            },
        ],
    }
    canonical = json.dumps(
        expected_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert snapshot.manifest == expected_manifest
    assert snapshot.digest == hashlib.sha256(canonical).hexdigest()
    verify_snapshot(snapshot)


@pytest.mark.parametrize(
    "path", ["", "/absolute", "../escape", "a/../b", "a//b", r"a\b", "C:/drive", "./x", "a\x00b"]
)
def test_path_validation_rejects_ambiguous_and_escaping_paths(path):
    with pytest.raises(LayerError) as error:
        validate_path(path)
    assert error.value.code == "PATH_UNSAFE"


def test_tree_rejects_case_collision_and_reserved_marker():
    with pytest.raises(LayerError, match="collide"):
        validate_tree({"Readme": TreeFile(b"a"), "README": TreeFile(b"b")})
    with pytest.raises(LayerError, match="reserved"):
        validate_tree({".skill-layers-export.json": TreeFile(b"{}")})
    validate_tree({".skill-layers-export.json": TreeFile(b"{}")}, allow_marker=True)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX executable masks are not represented by Windows ACLs"
)
def test_capture_preserves_bytes_and_portable_execute_bits(tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_bytes(b"---\r\nname: demo\r\n---\r\nhello\r\n")
    script = root / "scripts" / "run.sh"
    script.write_bytes(b"#!/bin/sh\necho ok\n")
    script.chmod(0o751)
    tree = capture_tree(root)
    assert tree["SKILL.md"].content == b"---\r\nname: demo\r\n---\r\nhello\r\n"
    assert tree["scripts/run.sh"].execute_bits == 0o111
    assert tree["scripts/run.sh"].executable is True


def test_capture_rejects_symlinks_hardlinks_and_special_entries(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"skill")
    (root / "link").symlink_to(root / "SKILL.md")
    with pytest.raises(LayerError) as symlink_error:
        capture_tree(root)
    assert symlink_error.value.code == "PATH_UNSAFE"
    (root / "link").unlink()
    os.link(root / "SKILL.md", root / "hardlink")
    with pytest.raises(LayerError) as hardlink_error:
        capture_tree(root)
    assert hardlink_error.value.code == "PATH_UNSAFE"


def test_capture_rejects_reserved_marker_unless_service_allows_exclusion(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"skill")
    (root / ".skill-layers-export.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LayerError):
        capture_tree(root)
    tree = capture_tree(root, allow_marker=True)
    assert ".skill-layers-export.json" not in tree
    assert "SKILL.md" in tree


def test_capture_detects_tree_change_during_scan(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"skill")
    real_listdir = os.listdir
    calls = 0

    def listdir(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "late.txt").write_bytes(b"late")
        return real_listdir(path)

    monkeypatch.setattr("skill_layers.snapshots.os.listdir", listdir)
    with pytest.raises(LayerError) as error:
        capture_tree(root)
    assert error.value.code == "STALE_INPUT"


@pytest.mark.skipif(os.name != "nt", reason="requires native Win32 handle capture")
def test_windows_capture_rejects_replacement_between_scan_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "SKILL.md"
    target.write_bytes(b"original content\n")
    create_handle = windows_capture._create_file_handle
    replaced = False

    def replace_before_leaf_open(path: str, access: int, share: int, flags: int) -> int:
        nonlocal replaced
        if flags == windows_capture.FILE_FLAG_OPEN_REPARSE_POINT and not replaced:
            old = root / "SKILL.old"
            os.replace(target, old)
            target.write_bytes(b"replacement has another length\n")
            replaced = True
        return create_handle(path, access, share, flags)

    monkeypatch.setattr(windows_capture, "_create_file_handle", replace_before_leaf_open)
    with pytest.raises(LayerError) as error:
        capture_tree(root)

    assert replaced
    assert error.value.code == "STALE_INPUT"
    assert target.read_bytes() == b"replacement has another length\n"


@pytest.mark.skipif(os.name == "nt", reason="Uses POSIX descriptor-relative directory opens")
def test_capture_uses_directory_handles_to_reject_ancestor_symlink_swap(tmp_path, monkeypatch):
    root = tmp_path / "source"
    child = root / "nested"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    (root / "SKILL.md").write_bytes(b"skill")
    (child / "file.txt").write_bytes(b"inside")
    (outside / "file.txt").write_bytes(b"outside")
    real_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            path == "nested"
            and kwargs.get("dir_fd") is not None
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            swapped = True
            (root / "nested").rename(root / "moved")
            (root / "nested").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("skill_layers.snapshots.os.open", swap_before_open)
    with pytest.raises(LayerError) as error:
        capture_tree(root)
    assert swapped
    assert error.value.code == "PATH_UNSAFE"


def test_write_tree_accepts_only_empty_stage_and_never_overwrites(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    tree = {"SKILL.md": TreeFile(b"new"), "scripts/run": TreeFile(b"x")}
    write_tree(stage, tree)
    assert (stage / "SKILL.md").read_bytes() == b"new"
    assert (stage / "scripts/run").read_bytes() == b"x"
    assert (stage / "scripts/run").stat().st_mode & 0o111 == 0
    with pytest.raises(LayerError):
        write_tree(stage, {"SKILL.md": TreeFile(b"overwrite")})
    assert (stage / "SKILL.md").read_bytes() == b"new"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX executable masks are not represented by Windows ACLs"
)
def test_write_tree_restores_exact_executable_mask(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    write_tree(stage, {"scripts/run": TreeFile(b"#!/bin/sh\n", True, 0o100)})
    assert (stage / "scripts/run").stat().st_mode & 0o111 == 0o100


def test_write_tree_refuses_symlinked_stage_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    stage = tmp_path / "stage"
    stage.symlink_to(real, target_is_directory=True)
    with pytest.raises(LayerError) as error:
        write_tree(stage, {"SKILL.md": TreeFile(b"no")})
    assert error.value.code == "PATH_UNSAFE"
    assert list(real.iterdir()) == []
