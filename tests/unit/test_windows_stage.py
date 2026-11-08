from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skill_layers.contracts import Code, LayerError, TreeFile
from skill_layers.windows_stage import stage_new

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="requires actual Windows Win32 handles")


def _junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(f"Required Windows junction creation failed: {result.stderr.strip()}")


@WINDOWS_ONLY
def test_stage_new_writes_binary_tree_under_unicode_path(tmp_path: Path) -> None:
    stage = tmp_path / "stage space – 日本語"
    payload = b"\x00\xffbinary\r\n"

    stage_new(
        stage,
        {"SKILL.md": TreeFile(b"# Synthetic\n"), "nested/data.bin": TreeFile(payload)},
        marker=b'{"format_version":1}\n',
    )

    assert (stage / "SKILL.md").read_bytes() == b"# Synthetic\n"
    assert (stage / "nested/data.bin").read_bytes() == payload
    assert (stage / ".skill-layers-export.json").read_bytes() == b'{"format_version":1}\n'
    assert (stage / "nested/data.bin").stat().st_nlink == 1


@WINDOWS_ONLY
def test_stage_new_refuses_nonempty_root_without_changing_it(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    sentinel = stage / "keep.txt"
    sentinel.write_bytes(b"preserve")

    with pytest.raises(LayerError) as error:
        stage_new(stage, {"new.txt": TreeFile(b"new")})

    assert error.value.code == Code.DESTINATION_EXISTS.value
    assert sentinel.read_bytes() == b"preserve"
    assert not (stage / "new.txt").exists()


@WINDOWS_ONLY
def test_stage_new_rejects_ancestor_junction_without_writing_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "ancestor-link"
    _junction(junction, outside)
    try:
        with pytest.raises(LayerError) as error:
            stage_new(junction / "stage", {"new.txt": TreeFile(b"blocked")})
        assert error.value.code == Code.PATH_UNSAFE.value
        assert not (outside / "stage").exists()
    finally:
        os.rmdir(junction)


@WINDOWS_ONLY
def test_stage_new_rejects_reparse_stage_root_without_writing_target(tmp_path: Path) -> None:
    target = tmp_path / "real-target"
    target.mkdir()
    junction = tmp_path / "stage-link"
    _junction(junction, target)
    try:
        with pytest.raises(LayerError) as error:
            stage_new(junction, {"new.txt": TreeFile(b"blocked")})
        assert error.value.code == Code.PATH_UNSAFE.value
        assert not (target / "new.txt").exists()
    finally:
        os.rmdir(junction)


def test_stage_new_fails_closed_off_windows(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("this assertion covers the non-Windows fail-closed branch")
    with pytest.raises(LayerError) as error:
        stage_new(tmp_path / "stage", {"file.txt": TreeFile(b"data")})
    assert error.value.code == Code.UNSUPPORTED_SOURCE.value
