"""Bounded, symlink-safe skill tree capture and content-addressed manifests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    FORMAT_VERSION,
    MARKER,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TREE_BYTES,
    LayerError,
    Snapshot,
    Tree,
    TreeFile,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPRESENTATIONS = {"raw", "normalized", "export"}
_VCS_NAMES = {".git", ".hg", ".svn", ".bzr"}
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
MAX_TREE_ENTRIES = 1024
MAX_PATH_DEPTH = 64


def validate_path(path: str) -> str:
    """Return a canonical relative POSIX file path or reject ambiguous/unsafe paths."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise LayerError("PATH_UNSAFE", "File paths must be non-empty relative paths.")
    if "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise LayerError("PATH_UNSAFE", f"Unsafe file path: {path!r}.")
    parts = path.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        raise LayerError("LIMIT_EXCEEDED", "Skill paths may be at most 64 components deep.")
    if any(part in {"", ".", ".."} for part in parts):
        raise LayerError("PATH_UNSAFE", f"Unsafe file path: {path!r}.")
    for part in parts:
        if part.casefold() == MARKER.casefold() and path != MARKER:
            raise LayerError("PATH_UNSAFE", "The ownership marker name is reserved.")
        if part.casefold() in _VCS_NAMES:
            raise LayerError("PATH_UNSAFE", "VCS metadata is not supported skill content.")
        stem = part.split(".", 1)[0].upper()
        if (
            part.endswith((" ", "."))
            or any(char in '<>:"|?*' for char in part)
            or stem in _WINDOWS_DEVICES
            or re.fullmatch(r"(?:COM|LPT)[0-9¹²³]", stem)
        ):
            raise LayerError("PATH_UNSAFE", f"File name is unsafe on Windows: {part!r}.")
        try:
            if len(part.encode("utf-8")) > 255:
                raise LayerError("PATH_UNSAFE", "A file name exceeds the portable byte limit.")
        except UnicodeEncodeError as exc:
            raise LayerError("PATH_UNSAFE", "File paths must be valid UTF-8.") from exc
    if unicodedata.normalize("NFC", path) != path:
        raise LayerError("PATH_UNSAFE", f"File path is not NFC-normalized: {path!r}.")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise LayerError("PATH_UNSAFE", f"Control characters are not allowed in paths: {path!r}.")
    if PurePosixPath(path).as_posix() != path:
        raise LayerError("PATH_UNSAFE", f"File path is not canonical: {path!r}.")
    return path


def validate_tree(tree: Tree, *, allow_marker: bool = False) -> None:
    """Validate tree paths, values, uniqueness and the frozen size limits."""
    if not isinstance(tree, dict):
        raise LayerError(
            "PATH_UNSAFE", "A skill tree must be a mapping of relative paths to files."
        )
    content_count = len(tree) - int(allow_marker and MARKER in tree)
    if content_count > MAX_FILES:
        raise LayerError("LIMIT_EXCEEDED", f"A skill may contain at most {MAX_FILES} files.")
    folded: dict[str, str] = {}
    path_kinds: dict[str, str] = {}
    total = 0
    for raw_path, file in tree.items():
        path = validate_path(raw_path)
        if path == MARKER:
            if allow_marker:
                continue
            raise LayerError(
                "PATH_UNSAFE", f"The reserved ownership file {MARKER!r} is not skill content."
            )
        if not isinstance(file, TreeFile) or not isinstance(file.content, bytes):
            raise LayerError("PATH_UNSAFE", f"Tree entry {path!r} is not a byte file.")
        if not isinstance(file.executable, bool):
            raise LayerError("PATH_UNSAFE", f"Tree entry {path!r} has invalid executable metadata.")
        if file.execute_bits is not None and (
            not isinstance(file.execute_bits, int)
            or file.execute_bits < 0
            or file.execute_bits & ~0o111
        ):
            raise LayerError("PATH_UNSAFE", f"Tree entry {path!r} has invalid execute bits.")
        segments = path.split("/")
        for index in range(1, len(segments) + 1):
            prefix = "/".join(segments[:index])
            key = unicodedata.normalize("NFC", prefix).casefold()
            kind = "file" if index == len(segments) else "directory"
            prior = folded.get(key)
            if prior is not None and (prior != prefix or path_kinds[key] != kind):
                raise LayerError(
                    "PATH_UNSAFE", f"Paths {prior!r} and {prefix!r} collide on common filesystems."
                )
            folded[key], path_kinds[key] = prefix, kind
        size = len(file.content)
        if size > MAX_FILE_BYTES:
            raise LayerError("LIMIT_EXCEEDED", f"File {path!r} exceeds {MAX_FILE_BYTES} bytes.")
        total += size
    if total > MAX_TREE_BYTES:
        raise LayerError(
            "LIMIT_EXCEEDED", f"A skill tree may contain at most {MAX_TREE_BYTES} bytes."
        )


def _stat_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    """Fields used only to detect capture races, never to identify snapshot content."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _is_reparse_point(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _regular_file(info: os.stat_result, display: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LayerError(
            "PATH_UNSAFE",
            f"Unsupported filesystem entry at {display!r}; "
            "only regular files and directories are allowed.",
        )
    if info.st_nlink != 1:
        raise LayerError("PATH_UNSAFE", f"Hard-linked files are not allowed: {display!r}.")


def _open_directory_chain(directory: Path) -> int:
    """Open an absolute POSIX directory without following any path component links."""
    if not hasattr(os, "O_NOFOLLOW") or not os.supports_dir_fd:
        raise LayerError(
            "UNSUPPORTED_SOURCE", "This platform lacks safe descriptor-relative skill capture."
        )
    absolute = Path(os.path.abspath(directory))
    fd = os.open(absolute.anchor or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def _capture_tree_windows(directory: Path, *, allow_marker: bool) -> Tree:
    """Path scan on Windows; file bytes come only from handle-verified opens."""
    from .windows_capture import open_verified

    input_root = Path(directory)
    root = input_root
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise LayerError("INVALID_SOURCE", "Cannot inspect selected skill directory.") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or _is_reparse_point(root_info)
        or not stat.S_ISDIR(root_info.st_mode)
    ):
        raise LayerError(
            "PATH_UNSAFE", "Selected skill root must be a real, non-reparse directory."
        )
    try:
        resolved_root = root.resolve(strict=True)
        resolved_info = resolved_root.lstat()
    except OSError as exc:
        raise LayerError(
            "INVALID_SOURCE", "Cannot safely resolve selected skill directory."
        ) from exc
    if _stat_fingerprint(root_info) != _stat_fingerprint(resolved_info):
        raise LayerError("STALE_INPUT", "Selected skill root changed during capture.", True)
    root = resolved_root
    captured: Tree = {}
    directory_names: dict[str, tuple[str, ...]] = {}
    observed: dict[str, tuple[int, ...]] = {"": _stat_fingerprint(root.lstat())}

    def walk(relative_dir: str, path_dir: Path) -> None:
        before = path_dir.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or not stat.S_ISDIR(before.st_mode)
        ):
            raise LayerError("PATH_UNSAFE", "A skill directory is a reparse point or changed type.")
        names = tuple(sorted(os.listdir(path_dir)))
        directory_names[relative_dir] = names
        for name in names:
            if len(observed) > MAX_TREE_ENTRIES:
                raise LayerError(
                    "LIMIT_EXCEEDED", "A skill may contain at most 1024 filesystem entries."
                )
            relative = validate_path(f"{relative_dir}/{name}" if relative_dir else name)
            child = path_dir / name
            try:
                info = child.lstat()
            except OSError as exc:
                raise LayerError(
                    "STALE_INPUT", f"Skill entry changed during capture: {relative!r}.", True
                ) from exc
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise LayerError("PATH_UNSAFE", f"Reparse points are not allowed: {relative!r}.")
            if relative == MARKER and allow_marker:
                _regular_file(info, relative)
                observed[relative] = _stat_fingerprint(info)
                continue
            if relative == MARKER:
                raise LayerError(
                    "PATH_UNSAFE", f"The reserved ownership file {MARKER!r} is not source content."
                )
            if stat.S_ISDIR(info.st_mode):
                observed[relative] = _stat_fingerprint(info)
                walk(relative, child)
                continue
            _regular_file(info, relative)
            if len(captured) >= MAX_FILES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"A skill may contain at most {MAX_FILES} files."
                )
            if info.st_size > MAX_FILE_BYTES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"File {relative!r} exceeds {MAX_FILE_BYTES} bytes."
                )
            try:
                with open_verified(child, root) as stream:
                    opened = os.fstat(stream.fileno())
                    if _stat_fingerprint(opened) != _stat_fingerprint(info):
                        raise LayerError(
                            "STALE_INPUT", f"Skill changed while opening {relative!r}.", True
                        )
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = stream.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise LayerError(
                                "LIMIT_EXCEEDED",
                                f"File {relative!r} exceeds {MAX_FILE_BYTES} bytes.",
                            )
                        chunks.append(chunk)
                    after = os.fstat(stream.fileno())
                after_path = child.lstat()
            except LayerError:
                raise
            except OSError as exc:
                raise LayerError(
                    "INVALID_SOURCE", f"Cannot safely read skill file {relative!r}."
                ) from exc
            if (
                _stat_fingerprint(opened) != _stat_fingerprint(after)
                or _stat_fingerprint(after) != _stat_fingerprint(after_path)
                or _is_reparse_point(after_path)
            ):
                raise LayerError("STALE_INPUT", f"Skill changed while reading {relative!r}.", True)
            execute_bits = info.st_mode & 0o111
            captured[relative] = TreeFile(b"".join(chunks), bool(execute_bits), execute_bits)
            observed[relative] = _stat_fingerprint(after)
            if sum(len(item.content) for item in captured.values()) > MAX_TREE_BYTES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"A skill tree may contain at most {MAX_TREE_BYTES} bytes."
                )
        after = path_dir.lstat()
        if _stat_fingerprint(before) != _stat_fingerprint(after) or _is_reparse_point(after):
            raise LayerError("STALE_INPUT", "Skill directory changed during capture.", True)

    walk("", root)
    for relative_dir, expected_names in directory_names.items():
        path_dir = root if not relative_dir else root.joinpath(*relative_dir.split("/"))
        try:
            if tuple(sorted(os.listdir(path_dir))) != expected_names:
                raise LayerError("STALE_INPUT", "Skill contents changed during capture.", True)
        except OSError as exc:
            raise LayerError(
                "STALE_INPUT", "Skill directory changed during capture.", True
            ) from exc
        for name in expected_names:
            relative = f"{relative_dir}/{name}" if relative_dir else name
            entry = root.joinpath(*relative.split("/"))
            actual = entry.lstat()
            if _is_reparse_point(actual) or _stat_fingerprint(actual) != observed.get(relative):
                raise LayerError(
                    "STALE_INPUT", f"Skill entry changed during capture: {relative!r}.", True
                )
    if _stat_fingerprint(input_root.lstat()) != observed[""]:
        raise LayerError("STALE_INPUT", "Selected skill root moved during capture.", True)
    validate_tree(captured)
    return captured


def capture_tree(directory: Path, *, allow_marker: bool = False) -> Tree:
    """Capture one stable subtree with descriptor-relative, no-follow POSIX traversal."""
    if os.name == "nt":
        return _capture_tree_windows(directory, allow_marker=allow_marker)
    input_root = Path(directory)
    try:
        root_info = input_root.lstat()
    except OSError as exc:
        raise LayerError("INVALID_SOURCE", f"Cannot inspect selected skill: {exc}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise LayerError("PATH_UNSAFE", "Selected skill root must be a real directory.")
    try:
        root = input_root.resolve(strict=True)
        resolved_info = root.lstat()
        if _stat_fingerprint(root_info) != _stat_fingerprint(resolved_info):
            raise LayerError("STALE_INPUT", "Selected skill root changed during capture.", True)
        root_fd = _open_directory_chain(root)
    except LayerError:
        raise
    except OSError as exc:
        code = "PATH_UNSAFE" if exc.errno in {errno.ELOOP, errno.EMLINK} else "INVALID_SOURCE"
        raise LayerError(code, "Cannot safely open selected skill directory.") from exc

    captured: Tree = {}
    directory_fds: dict[str, int] = {"": root_fd}
    directory_names: dict[str, tuple[str, ...]] = {}
    observed: dict[str, tuple[int, ...]] = {"": _stat_fingerprint(os.fstat(root_fd))}

    def walk(relative_dir: str, dir_fd: int) -> None:
        try:
            before_dir = os.fstat(dir_fd)
            names = tuple(sorted(os.listdir(dir_fd)))
        except OSError as exc:
            raise LayerError(
                "INVALID_SOURCE", f"Cannot read selected skill subtree: {exc}"
            ) from exc
        directory_names[relative_dir] = names
        for name in names:
            if len(observed) > MAX_TREE_ENTRIES:
                raise LayerError(
                    "LIMIT_EXCEEDED", "A skill may contain at most 1024 filesystem entries."
                )
            relative = validate_path(f"{relative_dir}/{name}" if relative_dir else name)
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise LayerError(
                    "STALE_INPUT", f"Skill entry changed during capture: {relative!r}.", True
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise LayerError("PATH_UNSAFE", f"Symbolic links are not allowed: {relative!r}.")
            if relative == MARKER and allow_marker:
                _regular_file(info, relative)
                observed[relative] = _stat_fingerprint(info)
                continue
            if relative == MARKER:
                raise LayerError(
                    "PATH_UNSAFE", f"The reserved ownership file {MARKER!r} is not source content."
                )
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd
                    )
                except OSError as exc:
                    raise LayerError(
                        "PATH_UNSAFE", f"Cannot safely open directory {relative!r}: {exc}"
                    ) from exc
                opened = os.fstat(child_fd)
                if _stat_fingerprint(opened) != _stat_fingerprint(info):
                    os.close(child_fd)
                    raise LayerError(
                        "STALE_INPUT", f"Skill directory changed while opening {relative!r}.", True
                    )
                directory_fds[relative] = child_fd
                observed[relative] = _stat_fingerprint(opened)
                walk(relative, child_fd)
                continue
            _regular_file(info, relative)
            if len(captured) >= MAX_FILES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"A skill may contain at most {MAX_FILES} files."
                )
            if info.st_size > MAX_FILE_BYTES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"File {relative!r} exceeds {MAX_FILE_BYTES} bytes."
                )
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                with os.fdopen(file_fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    _regular_file(opened, relative)
                    if _stat_fingerprint(opened) != _stat_fingerprint(info):
                        raise LayerError(
                            "STALE_INPUT", f"Skill changed while opening {relative!r}.", True
                        )
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = stream.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise LayerError(
                                "LIMIT_EXCEEDED",
                                f"File {relative!r} exceeds {MAX_FILE_BYTES} bytes.",
                            )
                        chunks.append(chunk)
                    after = os.fstat(stream.fileno())
                after_name = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except LayerError:
                raise
            except OSError as exc:
                code = (
                    "PATH_UNSAFE" if exc.errno in {errno.ELOOP, errno.EMLINK} else "INVALID_SOURCE"
                )
                raise LayerError(
                    code, f"Cannot safely read skill file {relative!r}: {exc}"
                ) from exc
            if _stat_fingerprint(opened) != _stat_fingerprint(after) or _stat_fingerprint(
                after
            ) != _stat_fingerprint(after_name):
                raise LayerError("STALE_INPUT", f"Skill changed while reading {relative!r}.", True)
            execute_bits = info.st_mode & 0o111
            captured[relative] = TreeFile(b"".join(chunks), bool(execute_bits), execute_bits)
            observed[relative] = _stat_fingerprint(after)
            if sum(len(item.content) for item in captured.values()) > MAX_TREE_BYTES:
                raise LayerError(
                    "LIMIT_EXCEEDED", f"A skill tree may contain at most {MAX_TREE_BYTES} bytes."
                )
        after_dir = os.fstat(dir_fd)
        if _stat_fingerprint(before_dir) != _stat_fingerprint(after_dir):
            raise LayerError(
                "STALE_INPUT",
                f"Skill directory changed during capture at {relative_dir or '.'}.",
                True,
            )

    try:
        walk("", root_fd)
        # Recheck names and entry identity through the same open directory handles. A renamed or
        # replaced ancestor cannot redirect these checks to a different filesystem tree.
        for relative_dir, expected_names in directory_names.items():
            dir_fd = directory_fds[relative_dir]
            if tuple(sorted(os.listdir(dir_fd))) != expected_names:
                raise LayerError(
                    "STALE_INPUT",
                    f"Skill contents changed during capture at {relative_dir or '.'}.",
                    True,
                )
            for name in expected_names:
                relative = f"{relative_dir}/{name}" if relative_dir else name
                actual = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                expected = observed.get(relative)
                if expected is None or _stat_fingerprint(actual) != expected:
                    raise LayerError(
                        "STALE_INPUT", f"Skill entry changed during capture: {relative!r}.", True
                    )
        current_root = input_root.lstat()
        if stat.S_ISLNK(current_root.st_mode) or _stat_fingerprint(
            current_root
        ) != _stat_fingerprint(os.fstat(root_fd)):
            raise LayerError("STALE_INPUT", "Selected skill root moved during capture.", True)
    except LayerError:
        raise
    except OSError as exc:
        raise LayerError("STALE_INPUT", f"Skill changed during capture: {exc}", True) from exc
    finally:
        for fd in directory_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
    validate_tree(captured)
    return captured


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LayerError("PATH_UNSAFE", f"Snapshot identity is not canonical JSON: {exc}") from exc


def make_snapshot(tree: Tree, representation: str, identity: dict | None = None) -> Snapshot:
    """Build a deterministic manifest whose digest identifies exactly this tree representation."""
    if representation not in _REPRESENTATIONS:
        raise LayerError("PATH_UNSAFE", f"Unknown snapshot representation {representation!r}.")
    validate_tree(tree)
    identity_value = {} if identity is None else identity
    if not isinstance(identity_value, dict):
        raise LayerError("PATH_UNSAFE", "Snapshot identity metadata must be an object.")
    identity_value = json.loads(_canonical_json(identity_value))
    files = []
    for path in sorted(tree):
        file = tree[path]
        execute_bits = (
            file.execute_bits
            if file.execute_bits is not None
            else (0o111 if file.executable else 0)
        )
        files.append(
            {
                "path": path,
                "sha256": hashlib.sha256(file.content).hexdigest(),
                "size": len(file.content),
                "executable": bool(execute_bits),
                "execute_bits": execute_bits,
            }
        )
    manifest = {
        "format_version": FORMAT_VERSION,
        "representation": representation,
        "identity": identity_value,
        "files": files,
    }
    digest = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    return Snapshot(
        digest=digest, representation=representation, manifest=manifest, tree=dict(tree)
    )


def verify_snapshot(snapshot: Snapshot) -> None:
    """Rebuild and compare the exact canonical manifest and digest."""
    if not isinstance(snapshot, Snapshot):
        raise LayerError("INTEGRITY_ERROR", "Expected a Snapshot record.")
    try:
        rebuilt = make_snapshot(
            snapshot.tree, snapshot.representation, snapshot.manifest.get("identity", {})
        )
    except (AttributeError, TypeError) as exc:
        raise LayerError("INTEGRITY_ERROR", f"Malformed snapshot: {exc}") from exc
    if rebuilt.manifest != snapshot.manifest or rebuilt.digest != snapshot.digest:
        raise LayerError("INTEGRITY_ERROR", "Snapshot manifest or digest does not match its tree.")


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES}:
            raise


def _write_tree_posix(target: Path, tree: Tree) -> None:
    """Build a stage with directory descriptors so replaced ancestors cannot redirect writes."""
    absolute = Path(os.path.abspath(target))
    parent = absolute.parent
    parent_fd = _open_directory_chain(parent)
    root_name = absolute.name
    root_created = False
    directory_fds: dict[str, int] = {}
    created_dirs: list[tuple[int, str]] = []
    created_files: list[tuple[int, str]] = []
    try:
        try:
            os.mkdir(root_name, mode=0o700, dir_fd=parent_fd)
            root_created = True
        except FileExistsError:
            info = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise LayerError("PATH_UNSAFE", "Stage root must be a real directory.") from None
        root_fd = os.open(root_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        directory_fds[""] = root_fd
        if os.listdir(root_fd):
            raise LayerError("DESTINATION_EXISTS", "Refusing to merge into a non-empty stage.")

        for relative in sorted(tree):
            parts = validate_path(relative).split("/")
            parent_key = ""
            for part in parts[:-1]:
                next_key = f"{parent_key}/{part}" if parent_key else part
                if next_key not in directory_fds:
                    parent_fd_for_child = directory_fds[parent_key]
                    os.mkdir(part, mode=0o755, dir_fd=parent_fd_for_child)
                    created_dirs.append((parent_fd_for_child, part))
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd_for_child,
                    )
                    directory_fds[next_key] = child_fd
                parent_key = next_key
            file = tree[relative]
            file_name = parts[-1]
            mode = 0o644 | (file.execute_bits or 0)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            parent_fd_for_file = directory_fds[parent_key]
            fd = os.open(file_name, flags, mode, dir_fd=parent_fd_for_file)
            created_files.append((parent_fd_for_file, file_name))
            with os.fdopen(fd, "wb") as stream:
                stream.write(file.content)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
        for fd in directory_fds.values():
            _fsync_fd(fd)
        _fsync_fd(parent_fd)
        final_info = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
        root_info = os.fstat(directory_fds[""])
        if _stat_fingerprint(final_info) != _stat_fingerprint(root_info):
            raise LayerError("STALE_INPUT", "Stage root changed while writing.", True)
    except Exception as exc:
        for file_parent_fd, file_name in reversed(created_files):
            try:
                os.unlink(file_name, dir_fd=file_parent_fd)
            except OSError:
                pass
        for dir_parent_fd, dir_name in reversed(created_dirs):
            try:
                os.rmdir(dir_name, dir_fd=dir_parent_fd)
            except OSError:
                pass
        if root_created:
            try:
                os.rmdir(root_name, dir_fd=parent_fd)
            except OSError:
                pass
        if isinstance(exc, LayerError):
            raise
        raise LayerError(
            "STORAGE_ERROR", f"Could not materialize new stage: {type(exc).__name__}."
        ) from exc
    finally:
        for fd in reversed(list(directory_fds.values())):
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(parent_fd)


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES}:
            raise


def write_tree(directory: Path, tree: Tree) -> None:
    """Materialize a complete tree at a new path or empty stage; never overwrite entries."""
    validate_tree(tree)
    target = Path(directory)
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and os.supports_dir_fd:
        _write_tree_posix(target, tree)
        return

    if os.name == "nt":
        from .windows_stage import stage_new

        stage_new(target, tree)
        return
    raise LayerError(
        "UNSUPPORTED_SOURCE", "Safe no-follow directory writes are unavailable on this platform."
    )
