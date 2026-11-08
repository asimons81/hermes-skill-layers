from __future__ import annotations

import pytest

from skill_layers.contracts import NEUTRAL_NAME, Code, LayerError, TreeFile
from skill_layers.identity import export_tree, normalize_tree, read_identity, transform_identity


def test_transform_preserves_all_bytes_outside_name_scalar_including_crlf() -> None:
    content = (
        b"---\r\n"
        b"name: old-skill\r\n"
        b'description: "Keep this quoted"\r\n'
        b"metadata:\r\n"
        b"  hermes:\r\n"
        b"    tags: [one, two]\r\n"
        b"---\r\n"
        b"# Heading\r\n"
        b"<<<<<<< a literal heading\r\n"
        b"---\r\n"
    )
    expected = content.replace(b"name: old-skill", b"name: sl-demo-layer-export", 1)

    assert read_identity(content) == "old-skill"
    assert transform_identity(content, "sl-demo-layer-export") == expected
    assert transform_identity(content, "sl-demo-layer-export") == expected


def test_transform_preserves_quotes_bom_metadata_and_utf8_body() -> None:
    content = (
        b"\xef\xbb\xbf---\n"
        b"name: 'source-name' # retain comment\n"
        b"author: Ren\xc3\xa9\n"
        b"---\n"
        b"Body: \xe2\x9c\x93\r\n"
    )
    expected = content.replace(b"'source-name'", b"'sl-copy-1'", 1)

    assert read_identity(content) == "source-name"
    assert transform_identity(content, "sl-copy-1") == expected


def test_normalize_export_round_trip_and_idempotence() -> None:
    original = b"---\nname: original\ndescription: sample\n---\nbody\n"
    tree = {
        "SKILL.md": TreeFile(original),
        "scripts/run.py": TreeFile(b"print('ok')\n", True, 0o100),
    }
    normalized = normalize_tree(tree)
    expected_neutral = original.replace(b"name: original", f"name: {NEUTRAL_NAME}".encode(), 1)

    assert normalized["SKILL.md"].content == expected_neutral
    assert normalized["scripts/run.py"] == tree["scripts/run.py"]
    assert normalize_tree(normalized) == normalized
    exported = export_tree(normalized, "sl-layer-export-abc")
    assert exported["SKILL.md"].content == expected_neutral.replace(
        NEUTRAL_NAME.encode(), b"sl-layer-export-abc", 1
    )
    assert exported["scripts/run.py"] == tree["scripts/run.py"]


def test_managed_name_mismatch_is_identity_changed() -> None:
    content = b"---\nname: edited-name\n---\nbody\n"

    with pytest.raises(LayerError) as caught:
        transform_identity(content, NEUTRAL_NAME, expected_name="sl-recorded-export")

    assert caught.value.code == Code.IDENTITY_CHANGED.value


@pytest.mark.parametrize(
    "frontmatter",
    [
        b"name: skill\nname: second\n",
        b"name: skill\nmetadata: !Custom value\n",
        b"name: &skill skill\nalias: *skill\n",
        b"name: [not, scalar]\n",
        b"description: missing\n",
        b"name: 17\n",
        b"name: |\n  multi-line\n",
        b'name: "unterminated\n',
    ],
)
def test_rejects_duplicate_tag_alias_or_unsupported_identity(frontmatter: bytes) -> None:
    content = b"---\n" + frontmatter + b"---\nbody\n"

    with pytest.raises(LayerError) as caught:
        read_identity(content)

    assert caught.value.code == Code.UNSUPPORTED_SOURCE.value


def test_rejects_missing_or_oversized_frontmatter() -> None:
    with pytest.raises(LayerError, match="frontmatter"):
        read_identity(b"plain text\n")

    with pytest.raises(LayerError) as caught:
        read_identity(b"x" * (2 * 1024 * 1024 + 1))
    assert caught.value.code == Code.LIMIT_EXCEEDED.value


@pytest.mark.parametrize("body", [b"body\xff\n", b"body\x00\n"])
def test_rejects_invalid_utf8_or_nul_in_body(body: bytes) -> None:
    content = b"---\nname: sample\n---\n" + body

    with pytest.raises(LayerError) as caught:
        read_identity(content)

    assert caught.value.code == Code.UNSUPPORTED_SOURCE.value


def test_rejects_frontmatter_over_byte_limit_before_yaml_construction() -> None:
    frontmatter = b"name: sample\nmetadata: " + b"x" * (64 * 1024)
    content = b"---\n" + frontmatter + b"\n---\nbody\n"

    with pytest.raises(LayerError, match="64 KiB") as caught:
        read_identity(content)

    assert caught.value.code == Code.UNSUPPORTED_SOURCE.value


def test_rejects_excessively_nested_frontmatter_before_yaml_construction() -> None:
    nested = b"metadata:\n" + b"".join(b"  " * level + b"child:\n" for level in range(1, 36))
    frontmatter = b"name: sample\n" + nested + b"  " * 36 + b"value: safe\n"
    content = b"---\n" + frontmatter + b"---\nbody\n"

    with pytest.raises(LayerError, match="nested too deeply") as caught:
        read_identity(content)

    assert caught.value.code == Code.UNSUPPORTED_SOURCE.value


@pytest.mark.parametrize("name", ["", "UPPER", "with space", "../escape", "a" * 65, "éclair"])
def test_rejects_non_generated_export_names(name: str) -> None:
    with pytest.raises(LayerError):
        transform_identity(b"---\nname: original\n---\nbody", name)


def test_transform_requires_recorded_neutral_name_for_export() -> None:
    content = b"---\nname: source\n---\nbody"

    with pytest.raises(LayerError) as caught:
        export_tree({"SKILL.md": TreeFile(content)}, "sl-demo-layer-export")

    assert caught.value.code == Code.IDENTITY_CHANGED.value


def test_normalize_requires_skill_md() -> None:
    with pytest.raises(LayerError) as caught:
        normalize_tree({"README.md": TreeFile(b"not a skill")})

    assert caught.value.code == Code.UNSUPPORTED_SOURCE.value
