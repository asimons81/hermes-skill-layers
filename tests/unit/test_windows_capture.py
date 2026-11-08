"""Windows-only integration tests for verified, same-handle file capture."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skill_layers import windows_capture
from skill_layers.contracts import Code, LayerError
from skill_layers.windows_capture import open_verified, stream_fingerprint

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="requires the real Win32 file APIs")


def _make_junction(junction: Path, target: Path) -> None:
    """Create the required NTFS test primitive without double-quoting cmd arguments."""
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        pytest.fail(f"Required Windows junction creation failed: {detail}")


def _remove_junction(path: Path) -> None:
    os.rmdir(path)


def test_non_windows_call_fails_closed_with_domain_error(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("this behavior is only the non-Windows fallback")
    root = tmp_path / "skill"
    root.mkdir()
    file = root / "SKILL.md"
    file.write_bytes(b"synthetic\n")

    with pytest.raises(LayerError) as raised:
        open_verified(file, root)

    assert raised.value.code == Code.UNSUPPORTED_SOURCE.value


@_WINDOWS_ONLY
def test_verified_open_supports_spaces_non_ascii_and_returns_readable_stream(
    tmp_path: Path,
) -> None:
    root = tmp_path / "技能 with spaces"
    root.mkdir()
    content = b"synthetic release notes\n\x00still bytes\n"
    target = root / "nested folder" / "naÃ¯ve.txt"
    target.parent.mkdir()
    target.write_bytes(content)

    with open_verified(target, root) as stream:
        assert stream.read() == content


@_WINDOWS_ONLY
def test_handle_fingerprint_is_stable_and_tracks_same_handle_changes(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    target = root / "entry.txt"
    target.write_bytes(b"before\n")

    with open_verified(target, root) as stream:
        before = stream_fingerprint(stream)
        assert stream_fingerprint(stream) == before
        target.write_bytes(b"after with a different size\n")
        after = stream_fingerprint(stream)

    assert after[:2] == before[:2]
    assert after != before


@_WINDOWS_ONLY
def test_verified_open_rejects_hardlinked_file(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"shared inode\n")
    linked = root / "linked.txt"
    try:
        os.link(target, linked)
    except OSError as exc:
        pytest.skip(f"hardlink creation unavailable on this Windows filesystem: {exc}")

    with pytest.raises(LayerError) as raised:
        open_verified(linked, root)

    assert raised.value.code == Code.PATH_UNSAFE.value


@_WINDOWS_ONLY
def test_verified_open_rejects_final_symlink_before_returning_stream(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"must not be returned\n")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable on this Windows runner: {exc}")

    with pytest.raises(LayerError) as raised:
        open_verified(link, root)

    assert raised.value.code == Code.PATH_UNSAFE.value


@_WINDOWS_ONLY
def test_verified_open_rejects_outside_and_inside_ancestor_junctions(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    outside = tmp_path / "outside"
    real = root / "real"
    root.mkdir()
    outside.mkdir()
    real.mkdir()
    (outside / "secret.txt").write_bytes(b"outside\n")
    (real / "secret.txt").write_bytes(b"inside alternative\n")

    outside_link = root / "outside-link"
    _make_junction(outside_link, outside)
    try:
        with pytest.raises(LayerError) as outside_error:
            open_verified(outside_link / "secret.txt", root)
        assert outside_error.value.code == Code.PATH_UNSAFE.value
    finally:
        _remove_junction(outside_link)

    inside_link = root / "inside-alias"
    _make_junction(inside_link, real)
    try:
        with pytest.raises(LayerError) as inside_error:
            open_verified(inside_link / "secret.txt", root)
        assert inside_error.value.code == Code.PATH_UNSAFE.value
    finally:
        _remove_junction(inside_link)


@_WINDOWS_ONLY
def test_final_handle_path_check_rejects_junction_swapped_after_ancestor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skill"
    original_parent = root / "section"
    outside = tmp_path / "outside"
    root.mkdir()
    original_parent.mkdir()
    outside.mkdir()
    (original_parent / "entry.txt").write_bytes(b"safe original\n")
    (outside / "entry.txt").write_bytes(b"outside target\n")

    create_handle = windows_capture._create_file_handle
    swapped = False

    def swap_before_leaf_open(path: str, access: int, share: int, flags: int) -> int:
        nonlocal swapped
        if flags == windows_capture.FILE_FLAG_OPEN_REPARSE_POINT and not swapped:
            moved = root / "section-original"
            os.replace(original_parent, moved)
            _make_junction(original_parent, outside)
            swapped = True
        return create_handle(path, access, share, flags)

    monkeypatch.setattr(windows_capture, "_create_file_handle", swap_before_leaf_open)
    try:
        with pytest.raises(LayerError) as raised:
            open_verified(original_parent / "entry.txt", root)
        assert swapped
        assert raised.value.code == Code.PATH_UNSAFE.value
    finally:
        monkeypatch.setattr(windows_capture, "_create_file_handle", create_handle)
        if original_parent.exists():
            _remove_junction(original_parent)
        moved = root / "section-original"
        if moved.exists():
            os.replace(moved, original_parent)
