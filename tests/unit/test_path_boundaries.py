from pathlib import Path

import pytest

from skill_layers.contracts import LayerError, TreeFile
from skill_layers.snapshots import capture_tree, validate_path, validate_tree


@pytest.mark.parametrize(
    "path",
    [
        "CON",
        "PRN.txt",
        "aux/report",
        "LPT9.log",
        "COM¹.txt",
        "x/clock$",
        "file:stream",
        "x?.md",
        "trailing.",
        "trailing ",
        ".git/config",
        "x/.Hg/store",
        "x/.svn/wc.db",
        "x/.skill-layers-export.json",
        ".SKILL-LAYERS-EXPORT.JSON",
        "bad\ud800",
    ],
)
def test_nonportable_names_rejected_before_filesystem_access(path):
    with pytest.raises(LayerError) as caught:
        validate_path(path)
    assert caught.value.code == "PATH_UNSAFE"


@pytest.mark.parametrize("paths", [("Refs/a.md", "refs/b.md"), ("refs", "refs/file.md")])
def test_directory_alias_and_file_directory_collisions_rejected(paths):
    with pytest.raises(LayerError) as caught:
        validate_tree({path: TreeFile(b"content") for path in paths})
    assert caught.value.code == "PATH_UNSAFE"


def test_vcs_directory_capture_rejected_without_returning_partial_tree(tmp_path: Path):
    (tmp_path / "SKILL.md").write_bytes(b"Synthetic")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"Private repository metadata")
    with pytest.raises(LayerError) as caught:
        capture_tree(tmp_path)
    assert caught.value.code == "PATH_UNSAFE"


def test_empty_directory_flood_is_bounded(tmp_path: Path):
    for index in range(1025):
        (tmp_path / f"dir-{index}").mkdir()
    with pytest.raises(LayerError) as caught:
        capture_tree(tmp_path)
    assert caught.value.code == "LIMIT_EXCEEDED"


def test_deep_path_is_bounded():
    with pytest.raises(LayerError) as caught:
        validate_tree({"/".join(["folder"] * 65): TreeFile(b"text")})
    assert caught.value.code == "LIMIT_EXCEEDED"
