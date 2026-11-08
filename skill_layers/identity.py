"""Byte-preserving validation and identity adaptation for ordinary Hermes skills."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import cast

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode
from ruamel.yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    ScalarToken,
    TagToken,
)

from .contracts import MAX_FILE_BYTES, NEUTRAL_NAME, Code, LayerError, Tree, TreeFile

_EXPORT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OPENING = (b"---\n", b"---\r\n")
MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_YAML_TOKENS = 512
MAX_YAML_NODES = 256
MAX_YAML_DEPTH = 32
_COLLECTION_STARTS = (
    FlowSequenceStartToken,
    FlowMappingStartToken,
    BlockSequenceStartToken,
    BlockMappingStartToken,
)
_COLLECTION_ENDS = (FlowSequenceEndToken, FlowMappingEndToken, BlockEndToken)


def _scan_bounded_yaml(text: str) -> None:
    token_count = 0
    node_count = 0
    depth = 0
    for token in YAML(typ="safe").scan(text):
        token_count += 1
        if token_count > MAX_YAML_TOKENS:
            raise LayerError(
                Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter has too many YAML tokens."
            )
        if isinstance(token, (TagToken, AnchorToken, AliasToken)):
            raise LayerError(
                Code.UNSUPPORTED_SOURCE, "Tagged or aliased frontmatter is unsupported."
            )
        if isinstance(token, _COLLECTION_STARTS):
            depth += 1
            node_count += 1
            if depth > MAX_YAML_DEPTH:
                raise LayerError(
                    Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter is nested too deeply."
                )
        elif isinstance(token, _COLLECTION_ENDS):
            depth = max(0, depth - 1)
        elif isinstance(token, ScalarToken):
            node_count += 1
        if node_count > MAX_YAML_NODES:
            raise LayerError(
                Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter has too many YAML nodes."
            )


def _frontmatter(content: bytes) -> tuple[str, int, int, str | None]:
    if len(content) > MAX_FILE_BYTES:
        raise LayerError(Code.LIMIT_EXCEEDED, "SKILL.md exceeds the file-size limit.")
    if b"\x00" in content:
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md must be UTF-8 text without NUL bytes.")
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "The complete SKILL.md file must be UTF-8."
        ) from exc

    prefix = b"\xef\xbb\xbf" if content.startswith(b"\xef\xbb\xbf") else b""
    body = content[len(prefix) :]
    opening_len = next((len(line) for line in _OPENING if body.startswith(line)), None)
    if opening_len is None:
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md must start with YAML frontmatter.")

    cursor = opening_len
    close_start: int | None = None
    close_end: int | None = None
    for line in body[opening_len:].splitlines(keepends=True):
        line_end = cursor + len(line)
        if line.rstrip(b"\r\n") == b"---":
            close_start, close_end = cursor, line_end
            break
        cursor = line_end
    if close_start is None or close_end is None:
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter has no closing delimiter.")

    raw_frontmatter = body[opening_len:close_start]
    if len(raw_frontmatter) > MAX_FRONTMATTER_BYTES:
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter exceeds the 64 KiB limit.")
    try:
        text = raw_frontmatter.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter must be UTF-8.") from exc

    # Explicit tags, anchors, and aliases are unnecessary for this small adapter and make
    # preservation/identity validation less predictable. Reject them before construction.
    try:
        _scan_bounded_yaml(text)

        loader = YAML(typ="safe")
        loader.allow_duplicate_keys = False
        loaded = loader.load(text)
        if not isinstance(loaded, dict):
            raise LayerError(
                Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter must be a YAML mapping."
            )
        if "name" not in loaded:
            raise LayerError(
                Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter requires a scalar name."
            )
        value = loaded["name"]
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
            raise LayerError(
                Code.UNSUPPORTED_SOURCE,
                "The frontmatter name must be a nonempty single-line string.",
            )

        root = YAML(typ="safe").compose(text)
    except LayerError:
        raise
    except Exception as exc:  # ruamel exposes several parser/constructor exception subclasses.
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md has invalid YAML frontmatter.") from exc

    if not isinstance(root, MappingNode):
        raise LayerError(Code.UNSUPPORTED_SOURCE, "SKILL.md frontmatter must be a YAML mapping.")
    name_nodes = [
        value_node
        for key_node, value_node in root.value
        if isinstance(key_node, ScalarNode) and key_node.value == "name"
    ]
    if len(name_nodes) != 1 or not isinstance(name_nodes[0], ScalarNode):
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "SKILL.md requires exactly one scalar top-level name."
        )
    node = cast(ScalarNode, name_nodes[0])
    if node.tag != "tag:yaml.org,2002:str" or node.start_mark.line != node.end_mark.line:
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "The frontmatter name must be a single-line string scalar."
        )
    start = node.start_mark.index
    end = node.end_mark.index
    scalar = text[start:end]
    if not scalar or "\n" in scalar or "\r" in scalar:
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "The frontmatter name must be a single-line scalar."
        )
    byte_start = len(text[:start].encode("utf-8")) + len(prefix) + opening_len
    byte_end = len(text[:end].encode("utf-8")) + len(prefix) + opening_len
    return value, byte_start, byte_end, node.style


def _validate_export_name(name: str) -> None:
    if not isinstance(name, str) or not _EXPORT_NAME.fullmatch(name):
        raise LayerError(
            Code.UNSUPPORTED_SOURCE,
            "Generated skill names must be 1–64 lowercase ASCII letters, digits, or hyphens, "
            "starting with a letter or digit.",
        )


def read_identity(content: bytes) -> str:
    """Validate ordinary single-scalar YAML frontmatter and return its name."""
    value, _, _, _ = _frontmatter(content)
    return value


def transform_identity(content: bytes, new_name: str, *, expected_name: str | None = None) -> bytes:
    """Replace only the YAML `name` scalar token, preserving all other bytes exactly."""
    _validate_export_name(new_name)
    old_name, start, end, style = _frontmatter(content)
    if expected_name is not None and old_name != expected_name:
        raise LayerError(
            Code.IDENTITY_CHANGED,
            "The skill's recorded generated identity no longer matches its frontmatter.",
            details={"expected": expected_name, "observed": old_name},
        )
    quote = style if style in ("'", '"') else ""
    replacement = f"{quote}{new_name}{quote}".encode()
    return content[:start] + replacement + content[end:]


def _transform_tree(tree: Tree, new_name: str, expected_name: str | None) -> Tree:
    skill = tree.get("SKILL.md")
    if not isinstance(skill, TreeFile):
        raise LayerError(
            Code.UNSUPPORTED_SOURCE, "A skill tree must contain a regular SKILL.md file."
        )
    transformed = dict(tree)
    transformed["SKILL.md"] = replace(
        skill, content=transform_identity(skill.content, new_name, expected_name=expected_name)
    )
    return transformed


def normalize_tree(tree: Tree, *, expected_name: str | None = None) -> Tree:
    """Return a copy whose accepted frontmatter identity is the reserved neutral name."""
    return _transform_tree(tree, NEUTRAL_NAME, expected_name)


def export_tree(tree: Tree, new_name: str) -> Tree:
    """Replace a normalized tree's neutral identity with a fresh generated skill name."""
    _validate_export_name(new_name)
    return _transform_tree(tree, new_name, NEUTRAL_NAME)
