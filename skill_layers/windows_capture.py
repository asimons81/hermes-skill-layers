"""Fail-closed Windows file opening for skill capture.

The returned stream owns the same Win32 handle that was checked for reparse points,
link count, and final path. No path-based read is performed here.
"""

from __future__ import annotations

import ctypes
import importlib
import ntpath
import os
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, cast

from .contracts import Code, LayerError

FILE_READ_ATTRIBUTES = 0x0080
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


@lru_cache(maxsize=1)
def _kernel32() -> Any:
    """Load and type the Windows API lazily so importing this module is portable."""
    if os.name != "nt":
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "Verified Windows capture is unavailable on this platform."
        )

    from ctypes import wintypes

    kernel_loader = getattr(ctypes, "WinDLL", None)
    if kernel_loader is None:
        raise LayerError(Code.DEPENDENCY_MISSING, "The Windows kernel API is unavailable.")
    kernel = kernel_loader("kernel32", use_last_error=True)
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
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _handle_value(handle: object) -> int | None:
    if handle is None:
        return None
    if isinstance(handle, int):
        return handle
    return ctypes.cast(cast(Any, handle), ctypes.c_void_p).value


def _create_file_handle(path: str, access: int, share: int, flags: int) -> int:
    kernel = _kernel32()
    handle = kernel.CreateFileW(
        path,
        access,
        share,
        None,
        OPEN_EXISTING,
        flags,
        None,
    )
    value = _handle_value(handle)
    if value is None or value in {-1, ctypes.c_void_p(-1).value}:
        raise _win32_error("Cannot safely open a skill path")
    return value


def _win32_error(message: str) -> LayerError:
    get_error = getattr(ctypes, "get_last_error", None)
    if get_error is None:
        raise LayerError(Code.DEPENDENCY_MISSING, "Windows error reporting is unavailable.")
    error = get_error()
    return LayerError(
        Code.INVALID_SOURCE,
        f"{message} (Win32 error {error}).",
        retryable=error in {2, 3, 32, 33, 87},
        details={"winerror": error},
    )


def _file_information(handle: int) -> Any:
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    if _kernel32().GetFileType(handle) != 0x0001:
        raise LayerError(Code.PATH_UNSAFE, "Only disk files and directories may be captured.")
    info = ByHandleFileInformation()
    if not _kernel32().GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _win32_error("Cannot inspect an opened skill path")
    return info


def _final_path(handle: int) -> str:
    kernel = _kernel32()
    size = 256
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            raise _win32_error("Cannot resolve the final path of an opened skill entry")
        if length < size:
            return _canonical_final_path(buffer.value)
        size = length + 1
    raise LayerError(
        Code.PATH_UNSAFE, "The verified Windows skill path exceeds the supported path limit."
    )


def _canonical_final_path(value: str) -> str:
    """Normalize separator/trailing slash only; preserve case-sensitive path components."""
    normalized = value.replace("/", "\\")
    while normalized.endswith("\\"):
        _drive, tail = ntpath.splitdrive(normalized)
        if tail == "\\":
            break
        normalized = normalized[:-1]
    return normalized


def _expected_child(parent: str, parts: tuple[str, ...]) -> str:
    return _canonical_final_path(ntpath.join(parent, *parts))


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        root_text = os.path.abspath(os.fspath(root))
        path_text = os.path.abspath(os.fspath(path))
        relative = ntpath.relpath(path_text, root_text)
    except (OSError, ValueError) as exc:
        raise LayerError(
            Code.PATH_UNSAFE, "The selected file is not below its captured skill root."
        ) from exc
    pure = PureWindowsPath(relative)
    parts = pure.parts
    if (
        not relative
        or relative == "."
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part or part.endswith((" ", ".")) or "\x00" in part for part in parts)
    ):
        raise LayerError(Code.PATH_UNSAFE, "The selected file path is not a safe relative path.")
    try:
        if ntpath.commonpath((root_text, path_text)) != ntpath.normpath(root_text):
            raise LayerError(
                Code.PATH_UNSAFE, "The selected file is not below its captured skill root."
            )
    except ValueError as exc:
        raise LayerError(
            Code.PATH_UNSAFE, "The selected file is not below its captured skill root."
        ) from exc
    return tuple(parts)


def _close_handles(handles: list[int]) -> None:
    kernel = _kernel32()
    for handle in reversed(handles):
        kernel.CloseHandle(handle)


def open_verified(path: Path, root: Path) -> BinaryIO:
    """Open one regular file below *root* after handle-based Win32 path verification.

    Reparse points are opened themselves (never followed) at each path component. Every
    directory handle remains open until the final file handle's normalized path has been
    compared to the root handle's path plus the requested relative components. Hard links
    and anything other than regular files are refused. The validated file handle is then
    transferred to the CRT and wrapped without reopening the pathname.
    """
    if os.name != "nt":
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "Verified Windows capture is unavailable on this platform."
        )

    relative_parts = _relative_parts(path, root)
    if not relative_parts:
        raise LayerError(Code.PATH_UNSAFE, "The skill root itself is not a regular skill file.")

    directory_handles: list[int] = []
    file_handle: int | None = None
    fd: int | None = None
    try:
        root_path = os.path.abspath(os.fspath(root))
        target_path = os.path.abspath(os.fspath(path))
        share = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        root_handle = _create_file_handle(
            root_path,
            FILE_READ_ATTRIBUTES,
            share,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        directory_handles.append(root_handle)
        root_info = _file_information(root_handle)
        if not root_info.attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise LayerError(Code.PATH_UNSAFE, "The captured skill root is not a directory.")
        if root_info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise LayerError(Code.PATH_UNSAFE, "The captured skill root is a reparse point.")
        root_final = _final_path(root_handle)

        for index, _component in enumerate(relative_parts[:-1], start=1):
            ancestor_path = ntpath.join(root_path, *relative_parts[:index])
            ancestor = _create_file_handle(
                ancestor_path,
                FILE_READ_ATTRIBUTES,
                share,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            )
            directory_handles.append(ancestor)
            info = _file_information(ancestor)
            if not info.attributes & FILE_ATTRIBUTE_DIRECTORY:
                raise LayerError(Code.PATH_UNSAFE, "A path component is not a directory.")
            if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise LayerError(
                    Code.PATH_UNSAFE, "Reparse-point directories are not allowed in a skill tree."
                )
            expected = _expected_child(root_final, relative_parts[:index])
            if _final_path(ancestor) != expected:
                raise LayerError(
                    Code.PATH_UNSAFE, "A skill directory resolves outside its expected path."
                )

        file_handle = _create_file_handle(
            target_path,
            GENERIC_READ | FILE_READ_ATTRIBUTES,
            share,
            FILE_FLAG_OPEN_REPARSE_POINT,
        )
        info = _file_information(file_handle)
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise LayerError(
                Code.PATH_UNSAFE, "Reparse-point files are not allowed in a skill tree."
            )
        if info.attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise LayerError(Code.PATH_UNSAFE, "Only regular files may be read from a skill tree.")
        if info.number_of_links != 1:
            raise LayerError(Code.PATH_UNSAFE, "Hard-linked files are not allowed in a skill tree.")
        expected_file = _expected_child(root_final, relative_parts)
        if _final_path(file_handle) != expected_file:
            raise LayerError(
                Code.PATH_UNSAFE, "The opened file resolves outside its expected skill path."
            )

        msvcrt = importlib.import_module("msvcrt")
        open_osfhandle = msvcrt.open_osfhandle
        binary_flag = getattr(os, "O_BINARY", 0)
        fd = open_osfhandle(file_handle, os.O_RDONLY | binary_flag)
        if fd == -1:
            fd = None
            raise LayerError(
                Code.INVALID_SOURCE, "Cannot transfer the verified file handle to Python."
            )
        file_handle = None  # open_osfhandle owns the OS handle from this point.
        stream = os.fdopen(fd, "rb")
        fd = None  # the returned stream owns the CRT file descriptor.
        return stream
    except LayerError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise LayerError(
            Code.INVALID_SOURCE,
            f"Cannot safely open selected skill file ({type(exc).__name__}).",
            retryable=True,
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if file_handle is not None:
            _kernel32().CloseHandle(file_handle)
        if directory_handles:
            _close_handles(directory_handles)
