"""Deterministic tree reconciliation and bounded Git text-merge adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from skill_layers.contracts import (
    MAX_FILE_BYTES,
    Code,
    Conflict,
    LayerError,
    MergeResult,
    Tree,
    TreeFile,
)

MERGE_TIMEOUT_SECONDS = 10.0
MERGE_OUTPUT_LIMIT = MAX_FILE_BYTES * 4 + 65_536


def git_available() -> str | None:
    """Return the resolved Git executable path, or None when Git is unavailable."""
    return shutil.which("git")


def _sanitized_env(git: str, home: Path) -> dict[str, str]:
    env = {
        "PATH": str(Path(git).resolve().parent),
        "HOME": str(home),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if os.name == "nt":
        # Windows process startup and Git's runtime need the operating-system root.
        for name in ("SystemRoot", "WINDIR"):
            if value := os.environ.get(name):
                env[name] = value
    return env


def _run_bounded(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, output_limit: int
) -> tuple[int, bytes, bytes, bool, bool]:
    """Drain both pipes concurrently, bounding retained bytes and killing overruns."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise LayerError(
            Code.DEPENDENCY_MISSING,
            "Git disappeared before merge-file could be started.",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise LayerError(
            Code.MERGE_ERROR,
            f"Could not start Git merge-file ({type(exc).__name__}).",
            retryable=True,
        ) from exc

    assert process.stdout is not None and process.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            captured = output[name]
            room = output_limit - len(captured)
            if len(chunk) > room:
                captured.extend(chunk[: max(0, room)])
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
            else:
                captured.extend(chunk)

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    for reader in readers:
        reader.join(timeout=2)
    return (
        process.returncode,
        bytes(output["stdout"]),
        bytes(output["stderr"]),
        overflow.is_set(),
        timed_out,
    )


def _git_merge(base: bytes, local: bytes, source: bytes) -> tuple[bytes, bool]:
    git = git_available()
    if git is None:
        raise LayerError(
            Code.DEPENDENCY_MISSING,
            "Git is required to merge independently edited text files.",
            retryable=False,
        )
    with tempfile.TemporaryDirectory(prefix="skill-layers-merge-") as temporary:
        root = Path(temporary)
        files = {"local": root / "local", "base": root / "base", "source": root / "source"}
        files["local"].write_bytes(local)
        files["base"].write_bytes(base)
        files["source"].write_bytes(source)
        result = _run_bounded(
            [
                git,
                "merge-file",
                "--stdout",
                "--diff3",
                "-L",
                "LOCAL",
                "-L",
                "BASE",
                "-L",
                "SOURCE",
                str(files["local"]),
                str(files["base"]),
                str(files["source"]),
            ],
            cwd=root,
            env=_sanitized_env(git, root),
            timeout=MERGE_TIMEOUT_SECONDS,
            output_limit=MERGE_OUTPUT_LIMIT,
        )
    returncode, stdout, stderr, overflow, timed_out = result
    if timed_out:
        raise LayerError(
            Code.MERGE_TIMEOUT,
            "Git merge-file exceeded the merge time limit.",
            retryable=True,
        )
    if overflow:
        raise LayerError(
            Code.MERGE_ERROR,
            "Git merge-file exceeded the bounded output limit; its partial output was discarded.",
            retryable=False,
            details={"reason": "output_limit", "limit_bytes_per_stream": MERGE_OUTPUT_LIMIT},
        )
    if returncode == 0:
        return stdout, False
    if 1 <= returncode <= 127:
        # Git reports a conflict count, saturating at 127. Never scan markers to infer state.
        return stdout, True
    diagnostic = stderr[:4096].decode("utf-8", errors="replace").strip()
    raise LayerError(
        Code.MERGE_ERROR,
        "Git merge-file failed" + (f": {diagnostic}" if diagnostic else "."),
        retryable=False,
        details={"returncode": returncode},
    )


def _is_utf8_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def _conflict(
    path: str,
    kind: str,
    base: TreeFile | None,
    local: TreeFile | None,
    source: TreeFile | None,
    proposal: TreeFile | None = None,
) -> Conflict:
    return Conflict(path=path, kind=kind, base=base, local=local, source=source, proposal=proposal)


def merge_trees(base: Tree, local: Tree, source: Tree) -> MergeResult:
    """Three-way merge per path; return every explicit conflict and Git proposal separately."""
    result: Tree = {}
    conflicts: dict[str, Conflict] = {}
    for path in sorted(set(base) | set(local) | set(source)):
        base_file = base.get(path)
        local_file = local.get(path)
        source_file = source.get(path)

        for file in (base_file, local_file, source_file):
            if file is not None and len(file.content) > MAX_FILE_BYTES:
                raise LayerError(
                    Code.LIMIT_EXCEEDED,
                    "A merge input file exceeds the supported file-size limit.",
                    details={"path": path, "limit_bytes": MAX_FILE_BYTES},
                )

        _validate_execute_support(base_file, local_file, source_file)
        if _same_file(local_file, base_file):
            if source_file is not None:
                result[path] = source_file
            continue
        if _same_file(source_file, base_file) or _same_file(local_file, source_file):
            if local_file is not None:
                result[path] = local_file
            continue

        if base_file is None:
            conflicts[path] = _conflict(path, "add_add", None, local_file, source_file)
            continue
        if local_file is None or source_file is None:
            conflicts[path] = _conflict(path, "delete_modify", base_file, local_file, source_file)
            continue

        merged_executable, mode_conflict = _merge_executable(
            _execute_bits(base_file), _execute_bits(local_file), _execute_bits(source_file)
        )
        content, text_conflict, binary_conflict = _merge_content(
            base_file.content, local_file.content, source_file.content
        )
        proposal = (
            TreeFile(content, bool(merged_executable), merged_executable)
            if content is not None
            else None
        )
        if mode_conflict:
            conflicts[path] = _conflict(path, "mode", base_file, local_file, source_file, proposal)
        elif binary_conflict:
            conflicts[path] = _conflict(path, "binary", base_file, local_file, source_file)
        elif text_conflict:
            conflicts[path] = _conflict(path, "text", base_file, local_file, source_file, proposal)
        elif proposal is not None:
            result[path] = proposal

    return MergeResult(tree=result, conflicts=conflicts)


def _execute_bits(file: TreeFile) -> int:
    bits = file.execute_bits if file.execute_bits is not None else (0o111 if file.executable else 0)
    if bits < 0 or bits & ~0o111:
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "Execute mode must contain only POSIX execute bits."
        )
    return bits


def _same_file(left: TreeFile | None, right: TreeFile | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.content == right.content and _execute_bits(left) == _execute_bits(right)


def _validate_execute_support(*files: TreeFile | None) -> None:
    if os.name != "nt":
        return
    for file in files:
        if file is not None and _execute_bits(file) not in (0, 0o111):
            raise LayerError(
                Code.UNSUPPORTED_SOURCE,
                "This Windows host cannot represent per-class POSIX execute bits.",
                details={"execute_bits": file.execute_bits},
            )


def _merge_executable(base: int, local: int, source: int) -> tuple[int, bool]:
    if local == base:
        return source, False
    if source == base or local == source:
        return local, False
    return local, True


def _merge_content(base: bytes, local: bytes, source: bytes) -> tuple[bytes | None, bool, bool]:
    if local == base:
        return source, False, False
    if source == base or local == source:
        return local, False, False
    if not all(_is_utf8_text(data) for data in (base, local, source)):
        return None, False, True
    proposal, conflict = _git_merge(base, local, source)
    return proposal, conflict, False
