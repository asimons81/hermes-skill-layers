"""The shared operator workflow and bounded, read-only agent views."""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import itertools
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

from .contracts import (
    NEUTRAL_NAME,
    Code,
    LayerError,
    ProfileScope,
    Record,
    Snapshot,
    Source,
    Tree,
    utc_now,
)
from .identity import export_tree, normalize_tree, read_identity
from .merge import git_available, merge_trees
from .publish import (
    canonical_json,
    destination,
    finish_rename,
    ownership,
    stage_export,
    staging_location,
    verify_marker,
)
from .snapshots import capture_tree, make_snapshot, validate_path, validate_tree
from .sources import Discovery, current_scope, source_from_record, source_to_record
from .store import Store

CAPTURE_LIMITATION = (
    "These are point-in-time captures. Later edits stay on the older export and are absent "
    "from the new one. Select the new skill explicitly; existing chats do not switch."
)
BEHAVIOR_LIMITATION = "A clean textual merge does not establish correct skill behavior."
PUBLICATION_BOUNDARY = (
    "Publish creates a new skill through this operator action. It does not enter Hermes' native "
    "skill_manage approval queue. Native edits retain their locks and approvals. Never use "
    "publication as a fallback for a denied native edit."
)


def _id() -> str:
    return str(uuid.uuid4())


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LayerError(Code.INVALID_STATE, "Expected a UUID identifier.") from exc


def _text_diff(path: str, left: str, right: str, budget: int | None) -> tuple[str, bool]:
    """Bound expensive matching; large files use an exact whole-file replacement diff."""
    if left == right:
        return "", False

    def lines(text: str) -> list[str]:
        parts = text.split("\n")
        return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

    old_lines, new_lines = lines(left), lines(right)
    chunks: Iterable[str]
    if len(left) + len(right) > 65_536 or len(old_lines) + len(new_lines) > 1000:
        chunks = itertools.chain(
            [
                f"--- before/{path}\n",
                f"+++ proposed/{path}\n",
                f"@@ -{1 if old_lines else 0},{len(old_lines)} "
                f"+{1 if new_lines else 0},{len(new_lines)} @@\n",
            ],
            ("-" + line for line in old_lines),
            ("+" + line for line in new_lines),
        )
    else:
        chunks = difflib.unified_diff(
            old_lines, new_lines, fromfile=f"before/{path}", tofile=f"proposed/{path}"
        )
    pieces = []
    for piece in chunks:
        if not piece.endswith("\n"):
            piece += "\n\\ No newline at end of file\n"
        if budget is not None and len(piece) > budget:
            pieces.append(piece[:budget])
            return "".join(pieces), True
        pieces.append(piece)
        if budget is not None:
            budget -= len(piece)
    return "".join(pieces), False


class Service:
    def __init__(
        self,
        scope: ProfileScope | None = None,
        *,
        readonly: bool = False,
        discovery: Discovery | None = None,
        scope_provider: Callable[[], ProfileScope] = current_scope,
        hooks: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        self.scope = scope or scope_provider()
        self.scope_provider = scope_provider
        self.store = Store(self.scope, readonly=readonly)
        self.readonly = readonly
        self.discovery = discovery or Discovery(self.scope)
        self.hooks = hooks or {}

    def close(self) -> None:
        self.store.close()

    def _hook(self, name: str) -> None:
        if callback := self.hooks.get(name):
            callback()

    def _scope_check(self, expected: str | None = None) -> None:
        live = self.scope_provider()
        if live.id != self.scope.id or (expected is not None and expected != live.id):
            raise LayerError(
                Code.PROFILE_MISMATCH, "The active profile changed; reopen this review."
            )
        if live.discovery_context != self.scope.discovery_context:
            raise LayerError(Code.SOURCE_MOVED, "The skill discovery context changed.")

    def _envelope(self, data: Record, operation_id: str | None = None) -> Record:
        result: Record = {
            "ok": True,
            "profile_id": self.scope.id,
            "profile": self.scope.public(),
            "data": data,
        }
        if operation_id is not None:
            result["operation_id"] = operation_id
        return result

    def _error(self, error: LayerError, operation_id: str | None = None) -> Record:
        result: Record = {
            "ok": False,
            "profile_id": self.scope.id,
            "profile": self.scope.public(),
            "error": error.to_dict(),
        }
        if operation_id is not None:
            result["operation_id"] = operation_id
        return result

    def read(self, action: str, identifier: str | None = None, *, bounded: bool = False) -> Record:
        """No storage initialization, migration, snapshot persistence, or recovery."""
        try:
            self._scope_check()
            data: Record
            if action == "doctor":
                data = {
                    "git": git_available(),
                    "native_publication_queue": False,
                    "cli_requires": "plugins.isolation: in_process",
                    "publication_boundary": PUBLICATION_BOUNDARY,
                    "behavior_notice": BEHAVIOR_LIMITATION,
                }
            elif action == "sources":
                data = {
                    "sources": [
                        {"id": item.id, "selector": item.selector, "name": item.display_name}
                        for item in self.discovery.sources()
                    ]
                }
            elif action == "list":
                rows = self.store.list("layers")
                data = {
                    "layers": rows[:100] if bounded else rows,
                    "truncated": bounded and len(rows) > 100,
                }
            elif action == "status":
                data = self._status(_uuid(identifier or ""), bounded=bounded)
            elif action in {"diff", "candidate"}:
                data = self._diff(_uuid(identifier or ""), bounded=bounded)
            else:
                raise LayerError(Code.INVALID_STATE, "Unknown read action.")
            self._scope_check()
            return self._envelope(data)
        except LayerError as exc:
            return self._error(exc)
        except (OSError, sqlite3.Error) as exc:
            return self._error(
                LayerError(
                    Code.STORAGE_ERROR,
                    "Cannot read the requested state.",
                    details={"errno": getattr(exc, "errno", None)},
                )
            )

    def mutate(
        self,
        action: str,
        parameters: Record,
        *,
        expected_profile: str,
        request_id: str | None = None,
    ) -> Record:
        operation: Record | None = None
        try:
            if self.readonly:
                raise LayerError(Code.PERMISSION_REQUIRED, "This service is read-only.")
            self._scope_check(expected_profile)
            request_id = _uuid(request_id) if request_id else _id()
            self.store.initialize()
            request_hash = _hash(
                {"action": action, "parameters": parameters, "profile": expected_profile}
            )
            with self.store.transaction():
                previous = next(
                    (op for op in self.store.list("operations") if op["request_id"] == request_id),
                    None,
                )
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise LayerError(
                            Code.IDEMPOTENCY_CONFLICT, "This request ID belongs to different input."
                        )
                    if previous.get("result") is not None:
                        return previous["result"]
                    if previous.get("error") is not None:
                        return previous["error"]
                    raise LayerError(
                        Code.RECOVERY_REQUIRED,
                        "This request is pending; inspect recovery.",
                        details={"operation_id": previous["id"]},
                    )
                operation = {
                    "id": _id(),
                    "request_id": request_id,
                    "request_hash": request_hash,
                    "profile_id": self.scope.id,
                    "action": action,
                    "candidate_id": None,
                    "destination": None,
                    "expected_snapshot": None,
                    "stage": None,
                    "phase": "preparing",
                    "result": None,
                    "error": None,
                    "created_at": utc_now(),
                }
                self.store.put("operations", operation["id"], operation)
            if action == "recover":
                with self.store.transaction():
                    data = self._recover(
                        parameters.get("operation_id"), active_operation=operation["id"]
                    )
                    self._complete_operation(operation, data)
            elif action == "derive":
                data = self._derive(parameters, operation)
            elif action == "checkpoint":
                data = self._checkpoint(parameters, operation)
            elif action in {"refresh", "rollback"}:
                data = self._reconcile(action, parameters, operation)
            elif action == "resolve":
                data = self._resolve(parameters, operation)
            elif action == "publish":
                data = self._publish(parameters, operation)
            else:
                raise LayerError(Code.INVALID_STATE, "Unknown mutation action.")
            self._hook("after_prepare_commit")
            with self.store.transaction():
                operation = self.store.get("operations", operation["id"])
                if operation["result"] is None:
                    self._complete_operation(operation, data)
            return operation["result"]
        except LayerError as exc:
            return self._failure(exc, operation)
        except (OSError, sqlite3.Error) as exc:
            return self._failure(
                LayerError(
                    Code.STORAGE_ERROR,
                    "Storage operation failed; inspect recovery before retrying.",
                    details={"errno": getattr(exc, "errno", None)},
                ),
                operation,
            )

    def _failure(self, error: LayerError, operation: Record | None) -> Record:
        response = self._error(error, operation["id"] if operation else None)
        if operation is None:
            return response
        response["request_id"] = operation["request_id"]
        try:
            with self.store.transaction():
                saved = self.store.get("operations", operation["id"])
                if (
                    saved["phase"] in {"intended", "staged"}
                    and error.code == Code.STALE_INPUT.value
                    and saved.get("destination")
                    and not Path(saved["destination"]).exists()
                ):
                    candidate = self.store.get("candidates", saved["candidate_id"])
                    candidate["state"] = "stale"
                    self.store.put("candidates", candidate["id"], candidate)
                    saved["phase"] = "failed"
                # Renamed exports remain recoverable; never turn them into abandoned failures.
                if saved["phase"] in {"preparing", "failed"}:
                    saved.update(phase="failed", error=response)
                self.store.put("operations", saved["id"], saved)
        except (LayerError, OSError, sqlite3.Error):
            response["error"]["details"]["journal_update_failed"] = True
        return response

    def _raw(self, directory: Path, *, export: Record | None = None) -> Snapshot:
        if export is not None:
            if directory != destination(self.scope, export["selector"]):
                raise LayerError(
                    Code.RECOVERY_REQUIRED, "The recorded export destination is invalid."
                )
            verify_marker(directory, ownership(export))
        tree = capture_tree(directory, allow_marker=export is not None)
        name = read_identity(tree["SKILL.md"].content) if "SKILL.md" in tree else ""
        if not name:
            raise LayerError(Code.UNSUPPORTED_SOURCE, "The skill requires a valid SKILL.md.")
        if export is not None and name != export["selector"]:
            raise LayerError(
                Code.IDENTITY_CHANGED, "The managed skill name changed; restore its recorded name."
            )
        return make_snapshot(tree, "raw", {"name": name})

    def _source(self, layer: Record) -> Source:
        return source_from_record(self.store.get("sources", layer["source_id"]))

    def _local(self, layer: Record) -> tuple[Record, Snapshot]:
        if not layer["preferred_export_id"]:
            raise LayerError(Code.INVALID_STATE, "Publish the first derivative before this action.")
        export = self.store.get("exports", layer["preferred_export_id"])
        return export, self._raw(Path(export["directory"]), export=export)

    def _complete_operation(self, operation: Record, data: Record) -> None:
        operation.update(
            phase="done",
            result=self._envelope({**data, "request_id": operation["request_id"]}, operation["id"]),
        )
        self.store.put("operations", operation["id"], operation)

    def _derive(self, parameters: Record, operation: Record) -> Record:
        label = str(parameters.get("label", "")).strip()
        try:
            label.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LayerError(Code.INVALID_STATE, "Labels must be valid UTF-8.") from exc
        if not label or len(label) > 100:
            raise LayerError(Code.INVALID_STATE, "A label of 1–100 characters is required.")
        source = self.discovery.lookup(str(parameters.get("source_id", "")))
        raw = self._raw(self.discovery.resolve(source))
        self.store.save_snapshot(raw)
        now = utc_now()
        layer: Record = {
            "id": _id(),
            "label": label,
            "source_id": source.id,
            "preferred_export_id": None,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        candidate = self._new_candidate(
            "derive", layer, raw, None, raw, raw.digest, normalize_tree(raw.tree)
        )
        record = source_to_record(source)
        record["latest_snapshot"] = raw.digest
        with self.store.transaction():
            self.store.put("sources", source.id, record)
            self.store.put("layers", layer["id"], layer)
            self.store.put("candidates", candidate["id"], candidate)
            self._complete_operation(operation, self._candidate_summary(candidate))
        return self._candidate_summary(candidate)

    def _new_candidate(
        self,
        kind: str,
        layer: Record,
        base: Snapshot,
        local: Snapshot | None,
        source: Snapshot | None,
        associated_base: str,
        tree: Tree,
        *,
        current: Snapshot | None = None,
        conflicts: Record | None = None,
    ) -> Record:
        export_id = _id()
        slug = re.sub(r"[^a-z0-9]+", "-", layer["label"].lower()).strip("-")[:22] or "skill"
        name = f"sl-{slug}-{layer['id'].replace('-', '')[:12]}-{export_id.replace('-', '')[:16]}"
        captured_current = current or local
        candidate: Record = {
            "id": _id(),
            "layer_id": layer["id"],
            "kind": kind,
            "base_snapshot": base.digest,
            "local_snapshot": local.digest if local else None,
            "source_snapshot": source.digest if source else None,
            "associated_base": associated_base,
            "inputs": {
                "layer_revision": layer["revision"],
                "preferred_export_id": layer["preferred_export_id"],
                "current_snapshot": captured_current.digest if captured_current else None,
                "discovery_context": self.scope.discovery_context,
            },
            "result_snapshot": "",
            "conflicts": conflicts or {},
            "resolutions": {},
            "revision": 1,
            "review_hash": "",
            "state": "ready",
            "export_id": export_id,
            "export_name": name,
            "reviewed_export_snapshot": None,
            "created_at": utc_now(),
            "operation_id": None,
        }
        return self._result(candidate, tree)

    def _result(self, candidate: Record, tree: Tree) -> Record:
        validate_tree(tree)
        result = make_snapshot(tree, "normalized", {"name": NEUTRAL_NAME})
        self.store.save_snapshot(result)
        candidate["result_snapshot"] = result.digest
        if candidate["conflicts"]:
            candidate.update(state="needs_resolution", reviewed_export_snapshot=None)
        else:
            output = export_tree(tree, candidate["export_name"])
            final = make_snapshot(output, "export", {"name": candidate["export_name"]})
            self.store.save_snapshot(final)
            candidate.update(state="ready", reviewed_export_snapshot=final.digest)
        candidate["review_hash"] = _hash(
            {
                "profile": self.scope.id,
                "id": candidate["id"],
                "revision": candidate["revision"],
                "inputs": candidate["inputs"],
                "result": result.digest,
                "conflicts": candidate["conflicts"],
                "export": candidate["reviewed_export_snapshot"],
                "name": candidate["export_name"],
            }
        )
        return candidate

    @staticmethod
    def _candidate_summary(candidate: Record) -> Record:
        return {
            "candidate_id": candidate["id"],
            "layer_id": candidate["layer_id"],
            "kind": candidate["kind"],
            "revision": candidate["revision"],
            "state": candidate["state"],
            "unresolved": sorted(candidate["conflicts"]),
            "next": "Review the complete candidate diff.",
        }

    def _checkpoint(self, parameters: Record, operation: Record) -> Record:
        layer = self.store.get("layers", _uuid(parameters.get("layer_id", "")))
        export, raw = self._local(layer)
        self.store.save_snapshot(raw)
        record = {
            "id": _id(),
            "export_id": export["id"],
            "layer_id": layer["id"],
            "captured_snapshot": raw.digest,
            "base_snapshot": export["base_snapshot"],
            "identity": export["selector"],
            "created_at": utc_now(),
        }
        with self.store.transaction():
            fresh = self.store.get("layers", layer["id"])
            if fresh["revision"] != layer["revision"]:
                raise LayerError(Code.STALE_INPUT, "The layer changed during capture.", True)
            self.store.put("checkpoints", record["id"], record)
            self._complete_operation(operation, {"checkpoint": record})
        return {"checkpoint": record}

    def _reconcile(self, action: str, parameters: Record, operation: Record) -> Record:
        layer = self.store.get("layers", _uuid(parameters.get("layer_id", "")))
        export, local = self._local(layer)
        self.store.save_snapshot(local)
        conflicts: Record = {}
        if action == "refresh":
            source_record = self._source(layer)
            source = self._raw(self.discovery.resolve(source_record))
            self.store.save_snapshot(source)
            base = self.store.load_snapshot(export["base_snapshot"])
            result = merge_trees(
                normalize_tree(base.tree),
                normalize_tree(local.tree, expected_name=export["selector"]),
                normalize_tree(source.tree),
            )
            tree = result.tree
            conflicts = {
                path: {
                    "kind": conflict.kind,
                    "local_available": conflict.local is not None,
                    "source_available": conflict.source is not None,
                }
                for path, conflict in result.conflicts.items()
            }
            proposed_local, associated = local, source.digest
        else:
            checkpoint = self.store.get("checkpoints", _uuid(parameters.get("checkpoint_id", "")))
            if checkpoint["layer_id"] != layer["id"]:
                raise LayerError(Code.INVALID_STATE, "That checkpoint belongs to another layer.")
            proposed_local = self.store.load_snapshot(checkpoint["captured_snapshot"])
            base = self.store.load_snapshot(checkpoint["base_snapshot"])
            tree = normalize_tree(proposed_local.tree, expected_name=checkpoint["identity"])
            source, associated = None, checkpoint["base_snapshot"]
        candidate = self._new_candidate(
            action,
            layer,
            base,
            proposed_local,
            source,
            associated,
            tree,
            current=local,
            conflicts=conflicts,
        )
        with self.store.transaction():
            if self.store.get("layers", layer["id"])["revision"] != layer["revision"]:
                raise LayerError(Code.STALE_INPUT, "The layer changed during preview.", True)
            self.store.put("candidates", candidate["id"], candidate)
            self._complete_operation(operation, self._candidate_summary(candidate))
        return self._candidate_summary(candidate)

    def _resolve(self, parameters: Record, operation: Record) -> Record:
        identifier = _uuid(parameters.get("candidate_id", ""))
        candidate = self.store.get("candidates", identifier)
        self._revision(candidate, parameters)
        if candidate["state"] not in {"ready", "needs_resolution"}:
            raise LayerError(Code.INVALID_STATE, "This candidate cannot be edited.")
        path = validate_path(str(parameters.get("path", "")))
        sides: dict[str, Tree] = {}
        known: set[str] = set()
        for side, key in (
            ("local", "local_snapshot"),
            ("source", "source_snapshot"),
            ("base", "base_snapshot"),
        ):
            sides[side] = (
                normalize_tree(self.store.load_snapshot(candidate[key]).tree)
                if candidate[key]
                else {}
            )
            known.update(sides[side])
        tree = dict(self.store.load_snapshot(candidate["result_snapshot"]).tree)
        known.update(tree)
        if path not in known:
            raise LayerError(
                Code.PATH_UNSAFE, "Resolution path is outside the candidate's known tree."
            )
        take = parameters.get("take")
        edited = parameters.get("edited")
        encoded = parameters.get("edited_base64")
        if sum(value is not None for value in (take, edited, encoded)) != 1:
            raise LayerError(Code.INVALID_STATE, "Supply exactly one whole-file resolution.")
        if edited is not None or encoded is not None:
            from .contracts import MAX_FILE_BYTES, TreeFile

            if encoded is not None:
                if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES + 2) // 3 * 4:
                    raise LayerError(
                        Code.LIMIT_EXCEEDED, "The edited input exceeds the file limit."
                    )
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise LayerError(
                        Code.INVALID_STATE, "Invalid encoded whole-file result."
                    ) from exc
            else:
                if not isinstance(edited, str):
                    raise LayerError(Code.INVALID_STATE, "Edited results must be UTF-8 text.")
                try:
                    content = edited.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise LayerError(
                        Code.INVALID_STATE, "Edited text must be valid UTF-8."
                    ) from exc
            if len(content) > MAX_FILE_BYTES:
                raise LayerError(Code.LIMIT_EXCEEDED, "The edited file exceeds the file limit.")
            executable = tree[path].executable if path in tree else False
            tree[path] = TreeFile(
                content, executable, tree[path].execute_bits if path in tree else 0
            )
            decision = "edited"
        elif take == "delete":
            tree.pop(path, None)
            decision = "delete"
        elif take in {"local", "source"} and path in sides[take]:
            tree[path] = sides[take][path]
            decision = take
        else:
            raise LayerError(Code.INVALID_STATE, "That resolution side is unavailable.")
        candidate["conflicts"].pop(path, None)
        candidate["resolutions"][path] = {"decision": decision, "at": utc_now()}
        candidate["revision"] += 1
        candidate = self._result(candidate, tree)
        with self.store.transaction():
            current = self.store.get("candidates", identifier)
            if current["revision"] != parameters.get("revision"):
                raise LayerError(Code.STALE_INPUT, "The candidate changed during resolution.", True)
            self.store.put("candidates", identifier, candidate)
            self._complete_operation(operation, self._candidate_summary(candidate))
        return self._candidate_summary(candidate)

    @staticmethod
    def _revision(candidate: Record, parameters: Record) -> None:
        if (
            type(parameters.get("revision")) is not int
            or parameters["revision"] != candidate["revision"]
        ):
            raise LayerError(
                Code.STALE_INPUT, "The review revision changed; request a new diff.", True
            )

    def _check_inputs(self, candidate: Record) -> None:
        self._scope_check()
        layer = self.store.get("layers", candidate["layer_id"])
        inputs = candidate["inputs"]
        if (
            layer["revision"] != inputs["layer_revision"]
            or layer["preferred_export_id"] != inputs["preferred_export_id"]
        ):
            raise LayerError(
                Code.STALE_INPUT, "The preferred export changed after this preview.", True
            )
        if inputs["current_snapshot"]:
            _, local = self._local(layer)
            if local.digest != inputs["current_snapshot"]:
                raise LayerError(
                    Code.STALE_INPUT, "The local copy changed after this preview.", True
                )
        if candidate["source_snapshot"]:
            source = self._raw(self.discovery.resolve(self._source(layer)))
            if source.digest != candidate["source_snapshot"]:
                raise LayerError(Code.STALE_INPUT, "The source changed after this preview.", True)

    def _publish(self, parameters: Record, operation: Record) -> Record:
        candidate = self.store.get("candidates", _uuid(parameters.get("candidate_id", "")))
        if candidate["state"] == "published":
            return {
                "export": self.store.get("exports", candidate["export_id"]),
                "notice": CAPTURE_LIMITATION,
            }
        self._revision(candidate, parameters)
        if candidate["conflicts"]:
            raise LayerError(
                Code.CONFLICTS_UNRESOLVED, "Resolve every recorded conflict before publication."
            )
        if candidate["state"] != "ready" or not candidate["reviewed_export_snapshot"]:
            raise LayerError(Code.INVALID_STATE, "This candidate is not ready for publication.")
        if parameters.get("review_hash") != candidate["review_hash"]:
            raise LayerError(
                Code.STALE_INPUT, "The reviewed result hash changed; request a new diff.", True
            )
        snapshot = self.store.load_snapshot(candidate["reviewed_export_snapshot"])
        validate_tree(snapshot.tree)
        if read_identity(snapshot.tree["SKILL.md"].content) != candidate["export_name"]:
            raise LayerError(Code.IDENTITY_CHANGED, "The prepared export identity is invalid.")
        target = destination(self.scope, candidate["export_name"])
        if target.exists() or target.is_symlink():
            raise LayerError(
                Code.DESTINATION_EXISTS, "The destination already exists; nothing replaced."
            )
        stage = staging_location(self.scope, self.store.root, operation["id"])
        export: Record = {
            "id": candidate["export_id"],
            "layer_id": candidate["layer_id"],
            "selector": candidate["export_name"],
            "directory": str(target),
            "initial_snapshot": snapshot.digest,
            "base_snapshot": candidate["associated_base"],
            "operation_id": operation["id"],
            "created_at": utc_now(),
        }
        with self.store.transaction():
            fresh = self.store.get("candidates", candidate["id"])
            self._revision(fresh, parameters)
            if fresh["state"] != "ready":
                raise LayerError(Code.STALE_INPUT, "Another publication owns this candidate.", True)
            candidate.update(state="publishing", operation_id=operation["id"])
            operation.update(
                candidate_id=candidate["id"],
                destination=str(target),
                expected_snapshot=snapshot.digest,
                stage=str(stage),
                phase="intended",
                export=export,
            )
            self.store.put("operations", operation["id"], operation)
            self.store.put("candidates", candidate["id"], candidate)
        self._hook("before_stage")
        stage_export(stage, snapshot.tree, ownership(export))
        with self.store.transaction():
            operation["phase"] = "staged"
            self.store.put("operations", operation["id"], operation)
        self._hook("before_final_check")
        with self.store.transaction():
            fresh = self.store.get("candidates", candidate["id"])
            if (
                fresh["operation_id"] != operation["id"]
                or fresh["revision"] != candidate["revision"]
            ):
                raise LayerError(
                    Code.STALE_INPUT, "The candidate changed before publication.", True
                )
            self._check_inputs(candidate)
            self._verify_staged_export(stage, export, snapshot.digest)
            self._hook("after_final_check")
            self._hook("before_rename")
            finish_rename(stage, target)
            self._hook("after_rename")
            self.discovery.verify_export(export["selector"], target)
            self._commit_export(candidate, operation, export)
        self._hook("after_db_commit")
        return {
            "export": export,
            "notice": CAPTURE_LIMITATION,
            "behavior_notice": BEHAVIOR_LIMITATION,
        }

    @staticmethod
    def _verify_staged_export(stage: Path, export: Record, expected_hash: str) -> None:
        verify_marker(stage, ownership(export))
        tree = capture_tree(stage, allow_marker=True)
        observed = make_snapshot(tree, "export", {"name": export["selector"]})
        if observed.digest != expected_hash:
            raise LayerError(
                Code.RECOVERY_REQUIRED, "The staged export differs from its reviewed tree."
            )

    def _commit_export(self, candidate: Record, operation: Record, export: Record) -> None:
        layer = self.store.get("layers", candidate["layer_id"])
        layer.update(
            preferred_export_id=export["id"], revision=layer["revision"] + 1, updated_at=utc_now()
        )
        candidate["state"] = "published"
        operation["phase"] = "committed"
        operation["result"] = self._envelope(
            {
                "export": export,
                "notice": CAPTURE_LIMITATION,
                "behavior_notice": BEHAVIOR_LIMITATION,
                "request_id": operation["request_id"],
            },
            operation["id"],
        )
        self.store.put("exports", export["id"], export)
        self.store.put("layers", layer["id"], layer)
        self.store.put("candidates", candidate["id"], candidate)
        self.store.put("operations", operation["id"], operation)

    def _recover(self, identifier: str | None, *, active_operation: str) -> Record:
        rows = self.store.list("operations")
        if identifier:
            identifier = _uuid(identifier)
            rows = [self.store.get("operations", identifier)]
        recovered = []
        for operation in rows:
            if operation["id"] == active_operation:
                continue
            _uuid(operation["id"])
            if operation["phase"] == "preparing":
                error = self._error(
                    LayerError(
                        Code.RECOVERY_REQUIRED,
                        "Preparation was interrupted before commit; use a new request ID.",
                    ),
                    operation["id"],
                )
                operation.update(phase="failed", error=error)
                self.store.put("operations", operation["id"], operation)
                recovered.append({"operation_id": operation["id"], "state": "failed_before_commit"})
                continue
            if operation["phase"] not in {"intended", "staged", "committed"}:
                continue
            if operation["phase"] == "committed":
                recovered.append({"operation_id": operation["id"], "state": "committed"})
                continue
            candidate = self.store.get("candidates", operation["candidate_id"])
            export = operation["export"]
            target = destination(self.scope, export["selector"])
            if str(target) != operation["destination"]:
                raise LayerError(
                    Code.RECOVERY_REQUIRED, "The operation destination is inconsistent."
                )
            expected_stage = staging_location(self.scope, self.store.root, operation["id"])
            if operation["stage"] != str(expected_stage):
                raise LayerError(
                    Code.RECOVERY_REQUIRED, "The operation staging location is inconsistent."
                )
            location = (
                target if target.exists() or target.is_symlink() else Path(operation["stage"])
            )
            verify_marker(location, ownership(export))
            # Stage capture also excludes its verified marker; never adopt on marker alone.
            if location != target:
                tree = capture_tree(location, allow_marker=True)
                raw = make_snapshot(tree, "export", {"name": export["selector"]})
            else:
                captured = self._raw(target, export=export)
                raw = make_snapshot(captured.tree, "export", {"name": export["selector"]})
            if raw.digest != operation["expected_snapshot"]:
                raise LayerError(
                    Code.RECOVERY_REQUIRED,
                    "The pending tree differs from its journal; preserve it for operator recovery.",
                )
            with self.store.transaction():
                current = self.store.get("candidates", candidate["id"])
                if current["state"] == "published":
                    recovered.append({"operation_id": operation["id"], "state": "committed"})
                    continue
                if current["operation_id"] != operation["id"]:
                    raise LayerError(
                        Code.RECOVERY_REQUIRED, "Another operation owns this candidate."
                    )
                # A post-rename crash may be completed only while layer ownership still agrees.
                layer = self.store.get("layers", candidate["layer_id"])
                if layer["revision"] != candidate["inputs"]["layer_revision"]:
                    raise LayerError(
                        Code.RECOVERY_REQUIRED, "The layer changed during interrupted publication."
                    )
                if location != target:
                    self._check_inputs(candidate)
                    finish_rename(location, target)
                self.discovery.verify_export(export["selector"], target)
                self._commit_export(candidate, operation, export)
            recovered.append({"operation_id": operation["id"], "state": "committed"})
        return {"operations": recovered}

    def _status(self, identifier: str, *, bounded: bool) -> Record:
        layer = self.store.get("layers", identifier)
        exports = [item for item in self.store.list("exports") if item["layer_id"] == identifier]
        checkpoints = [
            item for item in self.store.list("checkpoints") if item["layer_id"] == identifier
        ]
        candidates = [
            self._candidate_summary(item)
            for item in self.store.list("candidates")
            if item["layer_id"] == identifier
        ]
        result: Record = {
            "layer": layer,
            "exports": exports,
            "checkpoints": checkpoints,
            "candidates": candidates,
            "notice": CAPTURE_LIMITATION,
            "publication_boundary": PUBLICATION_BOUNDARY,
        }
        source_record = self._source(layer)
        result["source"] = {
            "id": source_record.id,
            "selector": source_record.selector,
            "display_name": source_record.display_name,
        }
        changes: Record = {"local": None, "source": None}
        if layer["preferred_export_id"]:
            preferred = self.store.get("exports", layer["preferred_export_id"])
            try:
                _, captured = self._local(layer)
                initial = self.store.load_snapshot(preferred["initial_snapshot"])
                changes["local"] = captured.tree != initial.tree
            except LayerError as exc:
                changes["local_error"] = exc.to_dict()
            try:
                captured_source = self._raw(self.discovery.resolve(self._source(layer)))
                changes["source"] = captured_source.digest != preferred["base_snapshot"]
                result["source"]["observed_name"] = captured_source.manifest["identity"]["name"]
            except LayerError as exc:
                changes["source_error"] = exc.to_dict()
        result["changes"] = changes
        if bounded:
            for key in ("exports", "checkpoints", "candidates"):
                result[key] = result[key][-25:]
        return result

    def _diff(self, identifier: str, *, bounded: bool) -> Record:
        candidate = self.store.get("candidates", identifier)
        if candidate["reviewed_export_snapshot"]:
            proposed = self.store.load_snapshot(candidate["reviewed_export_snapshot"]).tree
        else:
            proposed = self.store.load_snapshot(candidate["result_snapshot"]).tree
        old_hash = candidate["inputs"]["current_snapshot"]
        old = self.store.load_snapshot(old_hash).tree if old_hash else {}
        paths = sorted(set(old) | set(proposed))
        files = []
        remaining = 24_000 if bounded else None
        truncated = False
        for path in paths:
            before, after = old.get(path), proposed.get(path)
            if before == after and path not in candidate["conflicts"]:
                continue
            item: Record = {
                "path": path,
                "old_executable": before.executable if before else None,
                "new_executable": after.executable if after else None,
                "old_execute_bits": before.execute_bits if before else None,
                "new_execute_bits": after.execute_bits if after else None,
                "conflict": candidate["conflicts"].get(path),
                "old_sha256": hashlib.sha256(before.content).hexdigest() if before else None,
                "new_sha256": hashlib.sha256(after.content).hexdigest() if after else None,
            }
            try:
                left = before.content.decode("utf-8") if before else ""
                right = after.content.decode("utf-8") if after else ""
                if "\x00" in left or "\x00" in right:
                    raise UnicodeError()
                if any(
                    ord(char) < 32 and char not in "\t\r\n" for char in itertools.chain(left, right)
                ):
                    raise UnicodeError()
                text, shortened = _text_diff(path, left, right, remaining)
                truncated |= shortened
                if remaining is not None:
                    remaining -= len(text)
                item["diff"] = text
            except UnicodeError:
                item["binary"] = True
            files.append(item)
            if bounded and (len(files) >= 100 or remaining == 0):
                truncated = True
                break
        return {
            **self._candidate_summary(candidate),
            "kind": candidate["kind"],
            "files": files,
            "review_hash": candidate["review_hash"],
            "destination": str(destination(self.scope, candidate["export_name"])),
            "captured": {
                "base": candidate["base_snapshot"],
                "local": candidate["local_snapshot"],
                "source": candidate["source_snapshot"],
                "inputs": candidate["inputs"],
            },
            "publish_command": (
                f"hermes skill-layers publish {identifier} --revision {candidate['revision']} "
                f"--review-hash {candidate['review_hash']}"
            ),
            "truncated": truncated,
            "complete_review": not truncated,
            "notice": CAPTURE_LIMITATION,
            "behavior_notice": BEHAVIOR_LIMITATION,
            "publication_boundary": PUBLICATION_BOUNDARY,
            "published_selector": candidate["export_name"]
            if candidate["state"] == "published"
            else None,
        }
