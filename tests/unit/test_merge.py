from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skill_layers.contracts import Code, LayerError, TreeFile
from skill_layers.merge import git_available, merge_trees


def tree(content: bytes, executable: bool = False) -> dict[str, TreeFile]:
    return {"SKILL.md": TreeFile(content, executable)}


def test_t03_independent_local_and_source_edits_merge_exactly() -> None:
    base = b"title\nold local section\nkeep\nold source section\nend\n"
    local = b"title\ninclude migration notes\nkeep\nold source section\nend\n"
    source = b"title\nold local section\nkeep\ninclude issue links\nend\n"
    expected = b"title\ninclude migration notes\nkeep\ninclude issue links\nend\n"

    result = merge_trees(tree(base), tree(local), tree(source))

    assert result.tree == tree(expected)
    assert not result.conflicts


def test_text_conflict_keeps_raw_sides_and_diff3_proposal_separate() -> None:
    base = b"heading\n"
    local = b"local heading\n"
    source = b"source heading\n"

    result = merge_trees(tree(base), tree(local), tree(source))

    conflict = result.conflicts["SKILL.md"]
    assert conflict.kind == "text"
    assert conflict.base == TreeFile(base)
    assert conflict.local == TreeFile(local)
    assert conflict.source == TreeFile(source)
    assert conflict.proposal is not None
    assert conflict.proposal.content == (
        b"<<<<<<< LOCAL\nlocal heading\n||||||| BASE\nheading\n=======\n"
        b"source heading\n>>>>>>> SOURCE\n"
    )
    assert "SKILL.md" not in result.tree


def test_conflict_count_saturated_at_127_is_still_a_text_conflict() -> None:
    count = 132
    base = b"".join(f"base-{i}\nseparator-{i}\n".encode() for i in range(count))
    local = b"".join(f"local-{i}\nseparator-{i}\n".encode() for i in range(count))
    source = b"".join(f"source-{i}\nseparator-{i}\n".encode() for i in range(count))

    result = merge_trees(tree(base), tree(local), tree(source))

    conflict = result.conflicts["SKILL.md"]
    assert conflict.kind == "text"
    assert conflict.proposal is not None
    assert conflict.proposal.content.count(b"<<<<<<< LOCAL") == count


def test_marker_looking_text_and_markdown_underlines_do_not_create_conflict() -> None:
    base = (
        b"title\n"
        b"literal marker follows\n"
        b"<<<<<<< LOCAL\n=======\n>>>>>>> SOURCE\n"
        b"A markdown heading\n---\n"
        b"local section\n"
        b"context one\ncontext two\ncontext three\ncontext four\n"
        b"source section\n"
    )
    local = base.replace(b"local section", b"local improvement")
    source = base.replace(b"source section", b"source update")
    expected = base.replace(b"local section", b"local improvement").replace(
        b"source section", b"source update"
    )

    result = merge_trees(tree(base), tree(local), tree(source))

    assert not result.conflicts
    assert result.tree == tree(expected)


def test_crlf_noop_and_clean_merge_preserve_exact_line_endings() -> None:
    base = b"one\r\nkeep-a\r\nold-a\r\nkeep-b\r\nold-b\r\nend\r\n"
    local = b"one\r\nkeep-a\r\nlocal-a\r\nkeep-b\r\nold-b\r\nend\r\n"
    source = b"one\r\nkeep-a\r\nold-a\r\nkeep-b\r\nsource-b\r\nend\r\n"
    expected = b"one\r\nkeep-a\r\nlocal-a\r\nkeep-b\r\nsource-b\r\nend\r\n"

    assert merge_trees(tree(base), tree(base), tree(base)).tree == tree(base)
    result = merge_trees(tree(base), tree(local), tree(source))

    assert result.tree == tree(expected)
    assert not result.conflicts


@pytest.mark.parametrize(
    ("base", "local", "source", "kind"),
    [
        (None, b"local add\n", b"source add\n", "add_add"),
        (b"old\n", None, b"modified\n", "delete_modify"),
        (b"old\n", b"modified\n", None, "delete_modify"),
        (b"\x00base", b"\x00local", b"\x00source", "binary"),
        (b"\xffbase", b"\xfflocal", b"\xffsource", "binary"),
    ],
)
def test_explicit_non_text_conflict_kinds(
    base: bytes | None, local: bytes | None, source: bytes | None, kind: str
) -> None:
    result = merge_trees(
        {} if base is None else tree(base),
        {} if local is None else tree(local),
        {} if source is None else tree(source),
    )

    assert result.conflicts["SKILL.md"].kind == kind
    assert result.conflicts["SKILL.md"].proposal is None


def test_one_sided_changes_and_identical_addition_follow_equality_rules() -> None:
    assert merge_trees(tree(b"base"), tree(b"base"), tree(b"source")).tree == tree(b"source")
    assert merge_trees({}, tree(b"same"), tree(b"same")).tree == tree(b"same")
    assert merge_trees(tree(b"base"), {}, tree(b"base")).tree == {}


def test_executable_bit_from_one_side_merges_with_other_side_text_edit() -> None:
    base = tree(b"old\n", False)
    local = tree(b"old\n", True)
    source = tree(b"new\n", False)

    result = merge_trees(base, local, source)

    assert result.tree == {"SKILL.md": TreeFile(b"new\n", True, 0o111)}
    assert not result.conflicts


def test_incompatible_execute_masks_require_explicit_mode_resolution() -> None:
    base = {"SKILL.md": TreeFile(b"same\n", False, 0)}
    local = {"SKILL.md": TreeFile(b"same\n", True, 0o100)}
    source = {"SKILL.md": TreeFile(b"same\n", True, 0o010)}

    result = merge_trees(base, local, source)

    conflict = result.conflicts["SKILL.md"]
    assert conflict.kind == "mode"
    assert conflict.base == base["SKILL.md"]
    assert conflict.local == local["SKILL.md"]
    assert conflict.source == source["SKILL.md"]
    assert conflict.proposal == TreeFile(b"same\n", True, 0o100)


def test_merge_retains_execute_metadata_for_later_platform_validation() -> None:
    mode_source = {"SKILL.md": TreeFile(b"same\n", True, 0o100)}
    result = merge_trees({}, mode_source, mode_source)
    assert result.tree == mode_source
    assert not result.conflicts


def test_git_available_returns_executable_path() -> None:
    path = git_available()
    assert path is not None and Path(path).is_absolute()


def test_missing_git_is_a_dependency_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(LayerError) as caught:
        merge_trees(tree(b"base\n"), tree(b"local\n"), tree(b"source\n"))

    assert caught.value.code == Code.DEPENDENCY_MISSING.value


def _fake_git(monkeypatch: pytest.MonkeyPatch, path: Path, body: str) -> None:
    from skill_layers import merge

    program = path / "fake_git.py"
    program.write_text(body + "\n", encoding="utf-8")
    actual_runner = merge._run_bounded

    def run_python(argv, **options):
        return actual_runner([sys.executable, str(program), *argv[1:]], **options)

    monkeypatch.setattr(merge, "git_available", lambda: sys.executable)
    monkeypatch.setattr(merge, "_run_bounded", run_python)


def test_git_process_error_is_not_reported_as_a_merge_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_git(
        monkeypatch, tmp_path, "import sys; sys.stderr.write('simulated failure\\n'); sys.exit(255)"
    )
    with pytest.raises(LayerError) as caught:
        merge_trees(tree(b"base\n"), tree(b"local\n"), tree(b"source\n"))

    assert caught.value.code == Code.MERGE_ERROR.value
    assert caught.value.details["returncode"] == 255


def test_git_timeout_is_distinct_from_process_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_git(monkeypatch, tmp_path, "import time; time.sleep(2)")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("skill_layers.merge.MERGE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(LayerError) as caught:
        merge_trees(tree(b"base\n"), tree(b"local\n"), tree(b"source\n"))

    assert caught.value.code == Code.MERGE_TIMEOUT.value
    assert caught.value.retryable


def test_git_output_overflow_is_bounded_and_partial_output_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_git(monkeypatch, tmp_path, "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024))")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("skill_layers.merge.MERGE_OUTPUT_LIMIT", 128)

    with pytest.raises(LayerError) as caught:
        merge_trees(tree(b"base\n"), tree(b"local\n"), tree(b"source\n"))

    assert caught.value.code == Code.MERGE_ERROR.value
    assert caught.value.details["reason"] == "output_limit"
