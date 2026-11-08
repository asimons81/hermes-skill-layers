"""New-directory publication primitives. Never replace a source or existing export."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from .contracts import MARKER, Code, LayerError, ProfileScope, Record, Tree

NAME_RE = re.compile(r"sl-[a-z0-9-]{1,60}\Z")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sync_directory(path: Path) -> None:
    """Flush directory entries on POSIX. Windows file/rename durability needs host QA."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_new(source: Path, destination: Path) -> None:
    """Atomically rename a directory, refusing even an empty existing destination."""
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses an existing destination.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        arguments = (os.fsencode(source), os.fsencode(destination), 0x00000004)
        types = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        types = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    else:
        rename = None
        arguments = ()
        types = []
    if rename is None:
        raise LayerError(
            Code.DEPENDENCY_MISSING,
            "This platform lacks the required no-replace directory rename primitive.",
        )
    rename.argtypes = types
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def destination(scope: ProfileScope, name: str) -> Path:
    if len(name) > 64 or NAME_RE.fullmatch(name) is None:
        raise LayerError(Code.PATH_UNSAFE, "Invalid generated export name.")
    if scope.skills_dir.resolve() != scope.skills_dir:
        raise LayerError(Code.PROFILE_MISMATCH, "The profile skill destination moved.")
    return scope.skills_dir / name


def staging_location(scope: ProfileScope, data_root: Path, operation_id: str) -> Path:
    """Select a same-filesystem location outside every discovered skill root."""
    scope.skills_dir.mkdir(parents=True, exist_ok=True)
    device = scope.skills_dir.stat().st_dev
    for proposed in (
        data_root / "staging",
        scope.skills_dir.parent / ".skill-layers-staging",
        Path(tempfile.gettempdir()) / "skill-layers-staging",
    ):
        base = proposed.resolve()
        if any(base.is_relative_to(root.resolve()) for _, root in scope.roots):
            continue
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if base.stat().st_dev == device:
            return base / operation_id
    raise LayerError(
        Code.STORAGE_ERROR,
        "No same-filesystem staging location outside skill discovery is available.",
    )


def ownership(export: Record) -> Record:
    return {
        "format": 1,
        "export_id": export["id"],
        "layer_id": export["layer_id"],
        "operation_id": export["operation_id"],
        "selector": export["selector"],
        "initial_snapshot": export["initial_snapshot"],
    }


def verify_marker(directory: Path, expected: Record) -> None:
    try:
        root_info = directory.lstat()
        marker = directory / MARKER
        info = marker.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 16_384
        ):
            raise LayerError(Code.RECOVERY_REQUIRED, "Unsafe export ownership metadata.")
        raw = marker.read_bytes()
        if raw != canonical_json(expected):
            raise LayerError(Code.RECOVERY_REQUIRED, "Export ownership does not match its journal.")
    except OSError as exc:
        raise LayerError(
            Code.RECOVERY_REQUIRED, "Cannot verify export ownership metadata."
        ) from exc


def stage_export(stage: Path, tree: Tree, marker: Record) -> None:
    from .snapshots import write_tree

    if os.name == "nt":
        from .windows_stage import stage_new

        stage_new(stage, tree, canonical_json(marker))
        return
    stage.mkdir(mode=0o700, exist_ok=False)
    write_tree(stage, tree)
    with (stage / MARKER).open("xb") as handle:
        handle.write(canonical_json(marker))
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(stage)
    sync_directory(stage.parent)


def finish_rename(stage: Path, target: Path) -> None:
    try:
        if stage.stat().st_dev != target.parent.stat().st_dev:
            raise OSError(errno.EXDEV, "Staging and destination are on different filesystems")
        rename_new(stage, target)
        sync_directory(target.parent)
        sync_directory(stage.parent)
    except FileExistsError as exc:
        raise LayerError(
            Code.DESTINATION_EXISTS, "The destination already exists; nothing replaced."
        ) from exc
    except OSError as exc:
        raise LayerError(
            Code.STORAGE_ERROR,
            "Publication rename failed; source and earlier exports are preserved.",
            details={"errno": exc.errno},
        ) from exc
