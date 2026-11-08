"""Bind publication to directory objects rather than mutable pathname lookups."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import TracebackType

from .contracts import Code, LayerError


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class RenameGuard:
    def __init__(self, source: Path, target: Path) -> None:
        self.source, self.target = source, target
        self.source_fd: int | None = None
        self.target_fd: int | None = None
        self.windows_handles: list[int] = []
        self.parent_identities: list[tuple[Path, tuple[int, int]]] = []
        self.source_identity: tuple[int, int] | None = None

    def __enter__(self) -> RenameGuard:
        try:
            if os.name == "nt":
                from .windows_stage import hold_directory_chain

                self.windows_handles.extend(hold_directory_chain(self.source.parent))
                self.windows_handles.extend(hold_directory_chain(self.target.parent))
            else:
                from .snapshots import _open_directory_chain

                self.source_fd = _open_directory_chain(self.source.parent)
                self.target_fd = _open_directory_chain(self.target.parent)
            self.parent_identities = [
                (path, _identity(path.lstat())) for path in (self.source.parent, self.target.parent)
            ]
            source_info = self._source_stat()
            if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
                raise LayerError(
                    Code.RECOVERY_REQUIRED, "The staged export is not a real directory."
                )
            self.source_identity = _identity(source_info)
            self.verify()
            return self
        except BaseException:
            self.close()
            raise

    def _source_stat(self) -> os.stat_result:
        if self.source_fd is not None:
            return os.stat(self.source.name, dir_fd=self.source_fd, follow_symlinks=False)
        return self.source.lstat()

    def verify(self) -> None:
        try:
            for index, (path, expected) in enumerate(self.parent_identities):
                observed = path.lstat()
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or stat.S_ISLNK(observed.st_mode)
                    or path.resolve() != path
                    or _identity(observed) != expected
                ):
                    raise LayerError(Code.PROFILE_MISMATCH, "A publication parent directory moved.")
                descriptor = self.source_fd if index == 0 else self.target_fd
                if descriptor is not None and _identity(os.fstat(descriptor)) != expected:
                    raise LayerError(
                        Code.PROFILE_MISMATCH, "The bound publication directory changed."
                    )
            source = self._source_stat()
            if not stat.S_ISDIR(source.st_mode) or _identity(source) != self.source_identity:
                raise LayerError(
                    Code.RECOVERY_REQUIRED, "The staged directory changed before rename."
                )
        except OSError as exc:
            raise LayerError(
                Code.PROFILE_MISMATCH, "A publication directory is no longer available."
            ) from exc

    def close(self) -> None:
        for attribute in ("source_fd", "target_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)
        if self.windows_handles:
            from .windows_capture import _close_handles

            _close_handles(self.windows_handles)
            self.windows_handles.clear()

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
