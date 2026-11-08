"""Fail-closed Windows writer for a new or empty staged skill tree."""

from __future__ import annotations

import ctypes
import importlib
import ntpath
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from .contracts import (
    MARKER,
    Code,
    LayerError,
    Tree,
)
from .snapshots import validate_tree
from .windows_capture import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_READ_ATTRIBUTES,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    _canonical_final_path,
    _create_file_handle,
    _expected_child,
    _file_information,
    _final_path,
    _kernel32,
    _win32_error,
)

GENERIC_WRITE = 0x40000000
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x00000080


def _directory_api() -> Any:
    from ctypes import wintypes

    kernel = _kernel32()
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    return kernel


def _last_error() -> int:
    get_error = getattr(ctypes, "get_last_error", None)
    if get_error is None:
        raise LayerError(Code.DEPENDENCY_MISSING, "Windows error reporting is unavailable.")
    return int(get_error())


def _create_directory(path: str) -> bool:
    """Create one child directory; return False only when it already exists."""
    kernel = _directory_api()
    if kernel.CreateDirectoryW(path, None):
        return True
    error = _last_error()
    if error == 183:  # ERROR_ALREADY_EXISTS: caller reports a collision.
        return False
    raise _win32_error("Cannot create a staged skill directory")


def _create_new_file(path: str) -> int:
    from ctypes import wintypes

    kernel = _kernel32()
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(
        path,
        GENERIC_WRITE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
    if value is None or value in {-1, ctypes.c_void_p(-1).value}:
        error = _last_error()
        if error == 80:  # ERROR_FILE_EXISTS
            raise LayerError(Code.DESTINATION_EXISTS, "A staged skill file already exists.")
        if error == 183:  # ERROR_ALREADY_EXISTS
            raise LayerError(Code.DESTINATION_EXISTS, "A staged skill path already exists.")
        raise _win32_error("Cannot exclusively create a staged skill file")
    return int(value)


def _canonical_absolute(path: Path) -> str:
    try:
        raw = os.fspath(path)
        pure = PureWindowsPath(raw)
        if not pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
            raise ValueError("stage path must be absolute and contain no dot segments")
        value = os.path.abspath(raw)
        drive, tail = ntpath.splitdrive(value)
        if not drive or not ntpath.isabs(value):
            raise ValueError("stage path is not absolute")
        parts = [part for part in tail.split("\\") if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("stage path is not canonical")
        return ntpath.normpath(value)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerError(
            Code.PATH_UNSAFE, "The Windows stage path is not a safe absolute path."
        ) from exc


def _held_directory(path: str, expected: str | None) -> int:
    handle = _create_file_handle(
        path,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,  # deliberately omit FILE_SHARE_DELETE
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        info = _file_information(handle)
        if not info.attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise LayerError(Code.PATH_UNSAFE, "A staged skill path component is not a directory.")
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise LayerError(Code.PATH_UNSAFE, "Reparse-point directories cannot be staged.")
        if expected is not None and _final_path(handle) != _canonical_final_path(expected):
            raise LayerError(Code.PATH_UNSAFE, "A staged path component resolves unexpectedly.")
        return handle
    except Exception:
        _kernel32().CloseHandle(handle)
        raise


def _write_file(path: str, expected: str, content: bytes) -> None:
    handle = _create_new_file(path)
    fd: int | None = None
    stream = None
    try:
        info = _file_information(handle)
        if info.attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
            raise LayerError(Code.PATH_UNSAFE, "A staged file is not a plain file.")
        if info.number_of_links != 1:
            raise LayerError(Code.PATH_UNSAFE, "Hard-linked staged files are not allowed.")
        if expected is not None and _final_path(handle) != _canonical_final_path(expected):
            raise LayerError(Code.PATH_UNSAFE, "A staged file resolves outside its expected path.")
        msvcrt = importlib.import_module("msvcrt")
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        if fd == -1:
            fd = None
            raise LayerError(Code.STORAGE_ERROR, "Cannot transfer the verified stage file handle.")
        handle = -1  # the CRT descriptor now owns the Win32 handle.
        stream = os.fdopen(fd, "wb")
        fd = None
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    except LayerError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise LayerError(Code.STORAGE_ERROR, "Cannot safely write a staged skill file.") from exc
    finally:
        if stream is not None:
            stream.close()
        elif fd is not None:
            os.close(fd)
        if handle != -1:
            _kernel32().CloseHandle(handle)


def hold_directory_chain(directory: Path) -> list[int]:
    """Hold existing ancestors without delete sharing until the caller closes the handles."""
    target = _canonical_absolute(directory)
    drive, tail = ntpath.splitdrive(target)
    anchor = drive + "\\"
    handles: list[int] = []
    try:
        handle = _held_directory(anchor, None)
        handles.append(handle)
        path, final = anchor, _final_path(handle)
        for part in (item for item in tail.split("\\") if item):
            path = ntpath.join(path, part)
            handle = _held_directory(path, _expected_child(final, (part,)))
            handles.append(handle)
            final = _final_path(handle)
        return handles
    except BaseException:
        for handle in reversed(handles):
            _kernel32().CloseHandle(handle)
        raise


def stage_new(directory: Path, tree: Tree, marker: bytes | None = None) -> None:
    """Write *tree* into a securely held, new or empty Windows staging directory.

    The stage root may already exist only when it is a real empty directory. Every
    ancestor and created directory remains open without delete sharing until all
    exclusive file creations and writes have completed.
    """
    if os.name != "nt":
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "Safe Windows staging is unavailable on this platform."
        )
    validate_tree(tree)
    if marker is not None:
        if not isinstance(marker, bytes):
            raise LayerError(Code.PATH_UNSAFE, "The ownership marker must be bytes.")
        if len(marker) > 16_384:
            raise LayerError(
                Code.LIMIT_EXCEEDED, "The staged skill marker exceeds the portable size limit."
            )
    if any((entry.execute_bits or 0) for entry in tree.values()):
        raise LayerError(
            Code.UNSUPPORTED_SOURCE,
            "Windows staging cannot represent POSIX execute bits.",
        )

    target = _canonical_absolute(directory)
    drive, tail = ntpath.splitdrive(target)
    components = [part for part in tail.split("\\") if part]
    if not components:
        raise LayerError(Code.PATH_UNSAFE, "The Windows stage root cannot be a volume root.")
    anchor = drive + "\\"
    handles: list[int] = []
    directory_paths: dict[tuple[str, ...], str] = {(): target}
    try:
        anchor_handle = _held_directory(anchor, None)
        handles.append(anchor_handle)
        current_path = anchor
        current_final = _final_path(anchor_handle)
        # Validate and hold every existing parent before creating/opening the stage root.
        for component in components[:-1]:
            current_path = ntpath.join(current_path, component)
            expected = _expected_child(current_final, (component,))
            parent_handle = _held_directory(current_path, expected)
            handles.append(parent_handle)
            current_final = _final_path(parent_handle)

        root_path = ntpath.join(current_path, components[-1])
        root_expected = _expected_child(current_final, (components[-1],))
        _create_directory(root_path)
        root_handle = _held_directory(root_path, root_expected)
        handles.append(root_handle)
        try:
            if os.listdir(root_path):
                raise LayerError(Code.DESTINATION_EXISTS, "The Windows stage root is not empty.")
        except LayerError:
            raise
        except OSError as exc:
            raise LayerError(
                Code.STORAGE_ERROR, "Cannot verify that the Windows stage root is empty."
            ) from exc
        root_final = _final_path(root_handle)
        directory_paths[()] = root_path

        all_dirs = sorted(
            {
                tuple(path.split("/")[:-1][:index])
                for path in tree
                for index in range(1, len(path.split("/")))
            }
        )
        held_by_parts: dict[tuple[str, ...], int] = {(): root_handle}
        for parts in all_dirs:
            parent_parts = parts[:-1]
            parent_path = directory_paths[parent_parts]
            parent_handle = held_by_parts[parent_parts]
            expected_parent = _final_path(parent_handle)
            if expected_parent != _expected_child(root_final, parent_parts):
                raise LayerError(
                    Code.PATH_UNSAFE, "A staged directory parent changed unexpectedly."
                )
            child_path = ntpath.join(parent_path, parts[-1])
            if not _create_directory(child_path):
                raise LayerError(
                    Code.DESTINATION_EXISTS, "A staged skill directory already exists."
                )
            child_expected = _expected_child(expected_parent, (parts[-1],))
            child_handle = _held_directory(child_path, child_expected)
            handles.append(child_handle)
            held_by_parts[parts] = child_handle
            directory_paths[parts] = child_path

        content_files = [(path, item.content) for path, item in tree.items()]
        if marker is not None:
            content_files.append((MARKER, marker))
        for relative, content in sorted(content_files):
            parts = tuple(relative.split("/"))
            parent_parts = parts[:-1]
            parent_path = directory_paths[parent_parts]
            parent_handle = held_by_parts[parent_parts]
            parent_final = _final_path(parent_handle)
            if parent_final != _expected_child(root_final, parent_parts):
                raise LayerError(Code.PATH_UNSAFE, "A staged file parent changed unexpectedly.")
            file_path = ntpath.join(parent_path, parts[-1])
            expected_file = _expected_child(parent_final, (parts[-1],))
            _write_file(file_path, expected_file, content)
    finally:
        for handle in reversed(handles):
            _kernel32().CloseHandle(handle)
