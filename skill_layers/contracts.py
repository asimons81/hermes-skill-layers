"""Shared version-one domain contracts. These contain no host or I/O behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict

FORMAT_VERSION = 1
SCHEMA_VERSION = 1
MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 16 * 1024 * 1024
MARKER = ".skill-layers-export.json"
NEUTRAL_NAME = "skill-layers-identity"
Record = dict[str, Any]


class Code(StrEnum):
    INVALID_SOURCE = "INVALID_SOURCE"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    SOURCE_MOVED = "SOURCE_MOVED"
    PATH_UNSAFE = "PATH_UNSAFE"
    IDENTITY_CHANGED = "IDENTITY_CHANGED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    CONFLICTS_UNRESOLVED = "CONFLICTS_UNRESOLVED"
    STALE_INPUT = "STALE_INPUT"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    STORAGE_ERROR = "STORAGE_ERROR"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    MERGE_ERROR = "MERGE_ERROR"
    MERGE_TIMEOUT = "MERGE_TIMEOUT"


class LayerError(Exception):
    def __init__(
        self,
        code: str | Code,
        message: str,
        retryable: bool = False,
        details: Record | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value if isinstance(code, Code) else code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> Record:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class CandidateKind(StrEnum):
    DERIVE = "derive"
    REFRESH = "refresh"
    ROLLBACK = "rollback"


class CandidateState(StrEnum):
    NEEDS_RESOLUTION = "needs_resolution"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class TreeFile:
    content: bytes
    executable: bool = False
    execute_bits: int | None = None

    def __post_init__(self) -> None:
        bits = (
            self.execute_bits
            if self.execute_bits is not None
            else (0o111 if self.executable else 0)
        )
        if type(bits) is not int or bits < 0 or bits & ~0o111:
            raise ValueError("Only portable executable permission bits are supported")
        object.__setattr__(self, "execute_bits", bits)
        object.__setattr__(self, "executable", bool(bits))


Tree = dict[str, TreeFile]


@dataclass(frozen=True)
class Snapshot:
    digest: str
    representation: str
    manifest: Record
    tree: Tree


@dataclass(frozen=True)
class ProfileScope:
    id: str
    home: Path
    skills_dir: Path
    roots: tuple[tuple[int, Path], ...]
    discovery_context: str

    def public(self) -> Record:
        return {"id": self.id, "label": self.home.name, "schema_version": SCHEMA_VERSION}


@dataclass(frozen=True)
class Source:
    id: str
    selector: str
    root: Path
    relative: str
    display_name: str
    discovery_context: str
    latest_snapshot: str | None = None


@dataclass(frozen=True)
class Conflict:
    path: str
    kind: str
    base: TreeFile | None
    local: TreeFile | None
    source: TreeFile | None
    proposal: TreeFile | None = None


@dataclass(frozen=True)
class MergeResult:
    tree: Tree
    conflicts: dict[str, Conflict] = field(default_factory=dict)


class LayerRecord(TypedDict):
    id: str
    label: str
    source_id: str
    preferred_export_id: str | None
    revision: int
    created_at: str
    updated_at: str


class ExportRecord(TypedDict):
    id: str
    layer_id: str
    selector: str
    directory: str
    initial_snapshot: str
    base_snapshot: str
    operation_id: str
    created_at: str


class CheckpointRecord(TypedDict):
    id: str
    export_id: str
    layer_id: str
    captured_snapshot: str
    base_snapshot: str
    identity: str
    created_at: str


class CandidateRecord(TypedDict):
    id: str
    layer_id: str
    kind: str
    base_snapshot: str
    local_snapshot: str | None
    source_snapshot: str | None
    associated_base: str
    inputs: Record
    result_snapshot: str
    conflicts: Record
    resolutions: Record
    revision: int
    review_hash: str
    state: str
    export_id: str
    export_name: str
    reviewed_export_snapshot: str | None
    created_at: str
    operation_id: str | None


class OperationRecord(TypedDict):
    id: str
    request_id: str
    request_hash: str
    profile_id: str
    action: str
    candidate_id: str | None
    destination: str | None
    expected_snapshot: str | None
    stage: str | None
    phase: str
    result: Record | None
    error: Record | None
    created_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
