"""Cross-module safety contracts with independent synthetic trees and expected bytes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from skill_layers.contracts import (
    MARKER,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TREE_BYTES,
    NEUTRAL_NAME,
    Code,
    LayerError,
    Tree,
    TreeFile,
)
from skill_layers.identity import export_tree, normalize_tree, read_identity, transform_identity
from skill_layers.merge import merge_trees
from skill_layers.snapshots import (
    capture_tree,
    make_snapshot,
    validate_path,
    validate_tree,
    verify_snapshot,
    write_tree,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
RELEASE_NOTES = FIXTURES / "release_notes"
MERGE_CASES = FIXTURES / "merge_cases"
EXPORT_NAME = "sl-release-notes-fixture-9c72a1"


def _fixture_tree(path: Path) -> Tree:
    """Read committed fixture bytes independently of the implementation under test."""
    tree: Tree = {}
    for file in sorted(path.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(path).as_posix()
        execute_bits = file.stat().st_mode & 0o111 if os.name == "posix" else None
        tree[relative] = TreeFile(file.read_bytes(), execute_bits=execute_bits)
    return tree


def _layer_error(call, code: Code) -> None:
    with pytest.raises(LayerError) as raised:
        call()
    assert raised.value.code == code.value


def test_raw_snapshot_and_identity_transform_match_independent_golden_bytes() -> None:
    source_dir = RELEASE_NOTES / "v1"
    raw = capture_tree(source_dir)
    raw_snapshot = make_snapshot(raw, "raw", identity={"name": "release-notes"})
    verify_snapshot(raw_snapshot)
    assert raw_snapshot.representation == "raw"
    assert raw_snapshot.tree == _fixture_tree(source_dir)
    assert raw_snapshot.tree["SKILL.md"].content == (source_dir / "SKILL.md").read_bytes()
    manifest_files = {item["path"]: item for item in raw_snapshot.manifest["files"]}
    assert manifest_files["bin_render.sh"]["execute_bits"] == raw["bin_render.sh"].execute_bits

    normalized = normalize_tree(raw, expected_name="release-notes")
    normalized_snapshot = make_snapshot(
        normalized, "normalized", identity={"name": "release-notes"}
    )
    verify_snapshot(normalized_snapshot)
    assert normalized_snapshot.digest != raw_snapshot.digest
    assert normalized_snapshot.tree == _fixture_tree(RELEASE_NOTES / "expected_normalized_v1")
    assert normalize_tree(normalized, expected_name=NEUTRAL_NAME) == normalized

    exported = export_tree(normalized, EXPORT_NAME)
    assert exported == _fixture_tree(RELEASE_NOTES / "expected_export_v1")
    assert read_identity(exported["SKILL.md"].content) == EXPORT_NAME
    assert raw["SKILL.md"].content == (source_dir / "SKILL.md").read_bytes()


def test_identity_change_and_unsupported_yaml_fail_closed() -> None:
    source = (RELEASE_NOTES / "v1" / "SKILL.md").read_bytes()
    _layer_error(
        lambda: transform_identity(source, NEUTRAL_NAME, expected_name="another-skill"),
        Code.IDENTITY_CHANGED,
    )

    malformed = b"---\nname: first\nname: second\ndescription: test\n---\nBody\n"
    with pytest.raises(LayerError) as duplicate:
        read_identity(malformed)
    assert duplicate.value.code in {Code.INVALID_SOURCE.value, Code.UNSUPPORTED_SOURCE.value}

    tagged = b"---\nname: skill\nmetadata: !Custom value\n---\nBody\n"
    with pytest.raises(LayerError) as custom_tag:
        read_identity(tagged)
    assert custom_tag.value.code in {Code.INVALID_SOURCE.value, Code.UNSUPPORTED_SOURCE.value}


def test_path_validation_rejects_absolute_traversal_and_platform_separators() -> None:
    for unsafe in ("../outside", "a/../../outside", "/absolute/path", "a\\..\\outside", ""):
        _layer_error(lambda unsafe=unsafe: validate_path(unsafe), Code.PATH_UNSAFE)

    assert validate_path("references/format.md") == "references/format.md"


def test_tree_validation_rejects_reserved_and_case_colliding_paths() -> None:
    marker_tree = {MARKER: TreeFile(b"{}\n")}
    _layer_error(lambda: validate_tree(marker_tree), Code.PATH_UNSAFE)

    collision = {
        "Guide.md": TreeFile(b"Uppercase path\n"),
        "guide.md": TreeFile(b"Lowercase path\n"),
    }
    _layer_error(lambda: validate_tree(collision), Code.PATH_UNSAFE)


def test_capture_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside tree\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: hostile\ndescription: synthetic\n---\nBody\n", encoding="utf-8"
    )
    try:
        (source / "linked.txt").symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        if os.name == "nt":
            pytest.skip(f"symlink creation is unavailable on this Windows runner: {exc}")
        raise
    _layer_error(lambda: capture_tree(source), Code.PATH_UNSAFE)

    hardlink_root = tmp_path / "hardlink-source"
    hardlink_root.mkdir()
    (hardlink_root / "SKILL.md").write_text(
        "---\nname: hardlink\ndescription: synthetic\n---\nBody\n", encoding="utf-8"
    )
    target = tmp_path / "outside-hardlink.txt"
    target.write_text("shared inode\n", encoding="utf-8")
    try:
        os.link(target, hardlink_root / "shared.txt")
    except (NotImplementedError, OSError) as exc:
        if os.name == "nt":
            pytest.skip(f"hardlink creation is unavailable on this Windows runner: {exc}")
        raise
    _layer_error(lambda: capture_tree(hardlink_root), Code.PATH_UNSAFE)


def test_tree_caps_reject_oversized_file_count_and_total_bytes() -> None:
    too_many = {f"f-{index:03}.txt": TreeFile(b"x") for index in range(MAX_FILES + 1)}
    _layer_error(lambda: validate_tree(too_many), Code.LIMIT_EXCEEDED)

    large_file = {"large.bin": TreeFile(b"x" * (MAX_FILE_BYTES + 1))}
    _layer_error(lambda: validate_tree(large_file), Code.LIMIT_EXCEEDED)

    count = MAX_TREE_BYTES // MAX_FILE_BYTES + 2
    over_tree = {f"part-{index:03}.bin": TreeFile(b"x" * MAX_FILE_BYTES) for index in range(count)}
    _layer_error(lambda: validate_tree(over_tree), Code.LIMIT_EXCEEDED)


def test_write_tree_copies_untrusted_script_and_html_as_inert_bytes(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed.txt"
    script = (
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
    ).encode()
    html = b"<script>document.body.textContent='executed'</script>\n"
    source = tmp_path / "input"
    source.mkdir()
    (source / "SKILL.md").write_bytes(
        b"---\nname: inert-probe\ndescription: synthetic\n---\n"
        b"<script>alert('not executable')</script>\n"
    )
    (source / "run_me.py").write_bytes(script)
    (source / "example.html").write_bytes(html)

    captured = capture_tree(source)
    normalized = normalize_tree(captured, expected_name="inert-probe")
    make_snapshot(normalized, "normalized")
    merged = merge_trees(normalized, normalized, normalized)
    assert merged.conflicts == {}
    staged = tmp_path / "stage"
    write_tree(staged, merged.tree)

    assert (staged / "SKILL.md").read_bytes() == normalized["SKILL.md"].content
    assert (staged / "run_me.py").read_bytes() == script
    assert (staged / "example.html").read_bytes() == html
    assert not sentinel.exists()


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX execute bits are unavailable on this platform"
)
def test_write_tree_preserves_portable_execute_bits(tmp_path: Path) -> None:
    destination = tmp_path / "tree"
    write_tree(destination, {"run.sh": TreeFile(b"echo data\n", execute_bits=0o100)})

    assert destination.joinpath("run.sh").stat().st_mode & 0o111 == 0o100


def test_write_tree_refuses_to_merge_into_nonempty_stage(tmp_path: Path) -> None:
    destination = tmp_path / "already-there"
    destination.mkdir()
    sentinel = destination / "unrelated.txt"
    sentinel.write_bytes(b"preserve this unrelated file\n")

    _layer_error(
        lambda: write_tree(destination, {"SKILL.md": TreeFile(b"synthetic\n")}),
        Code.DESTINATION_EXISTS,
    )
    assert sentinel.read_bytes() == b"preserve this unrelated file\n"


def test_nonoverlapping_release_notes_changes_match_independent_merged_tree() -> None:
    base = normalize_tree(capture_tree(RELEASE_NOTES / "v1"), expected_name="release-notes")
    local = normalize_tree(capture_tree(RELEASE_NOTES / "local"), expected_name="release-notes")
    source = normalize_tree(capture_tree(RELEASE_NOTES / "v2"), expected_name="release-notes")

    result = merge_trees(base, local, source)

    assert result.conflicts == {}
    assert result.tree == _fixture_tree(RELEASE_NOTES / "expected_merged")


def test_literal_conflict_markers_and_markdown_underlines_do_not_create_conflicts() -> None:
    case = MERGE_CASES / "marker_text"
    base = {"notes.md": TreeFile((case / "base.md").read_bytes())}
    local = {"notes.md": TreeFile((case / "local.md").read_bytes())}
    source = {"notes.md": TreeFile((case / "source.md").read_bytes())}

    result = merge_trees(base, local, source)

    assert result.conflicts == {}
    merged = result.tree["notes.md"].content.decode("utf-8")
    assert "<<<<<<< this is ordinary fixture text" in merged
    assert "======= this is also ordinary fixture text" in merged
    assert ">>>>>>> and this is ordinary fixture text" in merged
    assert "-----------------------------------" in merged
    assert "Run the local preflight and record its output." in merged
    assert "Check the source release tag and installed version." in merged


def test_same_line_text_changes_record_conflict_even_with_marker_looking_content() -> None:
    case = MERGE_CASES / "conflict"
    base = {"notes.md": TreeFile((case / "base.md").read_bytes())}
    local = {"notes.md": TreeFile((case / "local.md").read_bytes())}
    source = {"notes.md": TreeFile((case / "source.md").read_bytes())}

    result = merge_trees(base, local, source)

    assert set(result.conflicts) == {"notes.md"}
    conflict = result.conflicts["notes.md"]
    assert conflict.kind == "text"
    assert conflict.base == base["notes.md"]
    assert conflict.local == local["notes.md"]
    assert conflict.source == source["notes.md"]
    assert conflict.proposal is not None


def test_add_add_and_delete_modify_are_explicit_conflicts() -> None:
    additions = MERGE_CASES / "add_add"
    add_result = merge_trees(
        {},
        {"guide.md": TreeFile((additions / "local/guide.md").read_bytes())},
        {"guide.md": TreeFile((additions / "source/guide.md").read_bytes())},
    )
    assert add_result.conflicts["guide.md"].kind == "add_add"

    delete_modify = MERGE_CASES / "delete_modify"
    delete_result = merge_trees(
        {"retiring.md": TreeFile((delete_modify / "base/retiring.md").read_bytes())},
        {},
        {"retiring.md": TreeFile((delete_modify / "source/retiring.md").read_bytes())},
    )
    assert delete_result.conflicts["retiring.md"].kind == "delete_modify"


def test_rename_is_an_ordinary_delete_plus_add() -> None:
    result = merge_trees(
        {"before.md": TreeFile(b"Original file.\n")},
        {"after.md": TreeFile(b"Original file with local edit.\n")},
        {"before.md": TreeFile(b"Original file.\n")},
    )

    assert result.conflicts == {}
    assert set(result.tree) == {"after.md"}
    assert result.tree["after.md"].content == b"Original file with local edit.\n"


def test_independent_executable_intent_survives_source_text_change() -> None:
    case = MERGE_CASES / "mode"
    base_bytes = (case / "base/README.md").read_bytes()
    base = {"README.md": TreeFile(base_bytes, execute_bits=0)}
    local = {"README.md": TreeFile(base_bytes, execute_bits=0o100)}
    source = {"README.md": TreeFile((case / "source/README.md").read_bytes(), execute_bits=0)}

    result = merge_trees(base, local, source)

    assert result.conflicts == {}
    assert result.tree["README.md"].content == source["README.md"].content
    assert result.tree["README.md"].execute_bits == 0o100


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX execute bits are unavailable on this platform"
)
def test_incompatible_execute_bit_changes_record_a_mode_conflict() -> None:
    base_content = b"Shared text.\n"
    base = {"run.sh": TreeFile(base_content, execute_bits=0)}
    local = {"run.sh": TreeFile(base_content, execute_bits=0o100)}
    source = {"run.sh": TreeFile(base_content, execute_bits=0o010)}

    result = merge_trees(base, local, source)

    assert set(result.conflicts) == {"run.sh"}
    conflict = result.conflicts["run.sh"]
    assert conflict.kind == "mode"
    assert conflict.base.execute_bits == 0
    assert conflict.local.execute_bits == 0o100
    assert conflict.source.execute_bits == 0o010


def test_independently_changed_binary_file_is_not_text_merged() -> None:
    case = MERGE_CASES / "binary"
    result = merge_trees(
        {"payload.bin": TreeFile((case / "base/payload.bin").read_bytes())},
        {"payload.bin": TreeFile((case / "local/payload.bin").read_bytes())},
        {"payload.bin": TreeFile((case / "source/payload.bin").read_bytes())},
    )

    assert result.conflicts["payload.bin"].kind == "binary"
    assert result.conflicts["payload.bin"].proposal is None
