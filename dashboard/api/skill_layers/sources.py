"""Profile-bound discovery of on-disk Hermes skills through public host helpers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts import MARKER, LayerError, ProfileScope, Source


def _canonical(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(data).hexdigest()


def current_scope() -> ProfileScope:
    """Resolve profile and skill discovery paths now, never from cached launch state."""
    try:
        from hermes_constants import get_hermes_home
        from tools.skills_tool import skill_search_dirs
    except Exception as exc:
        raise LayerError(
            "STORAGE_ERROR", f"Hermes skill discovery helpers are unavailable: {exc}"
        ) from exc
    try:
        home = _canonical(Path(get_hermes_home()))
        roots_raw, skills_dir_raw = skill_search_dirs()
        skills_dir = _canonical(Path(skills_dir_raw))
        roots = tuple((int(tier), _canonical(Path(root))) for tier, root in roots_raw)
    except Exception as exc:
        raise LayerError(
            "STORAGE_ERROR", f"Could not resolve the active Hermes profile: {exc}"
        ) from exc
    profile_id = "profile-" + _digest([str(home), str(skills_dir)])[:32]
    try:
        cwd = _canonical(Path.cwd())
    except OSError as exc:
        raise LayerError(
            "STORAGE_ERROR", f"Could not resolve the discovery working directory: {exc}"
        ) from exc
    stable_roots = [[tier, str(root)] for tier, root in roots if root != skills_dir]
    discovery_context = "discovery-" + _digest([str(home), str(skills_dir), stable_roots, str(cwd)])
    return ProfileScope(profile_id, home, skills_dir, roots, discovery_context)


def source_to_record(source: Source) -> dict[str, Any]:
    """Serialize a verified source identity without relying on a path-shaped external identifier."""
    return {
        "id": source.id,
        "selector": source.selector,
        "root": str(source.root),
        "relative": source.relative,
        "display_name": source.display_name,
        "discovery_context": source.discovery_context,
        "latest_snapshot": source.latest_snapshot,
    }


def source_from_record(record: dict[str, Any]) -> Source:
    """Rehydrate a stored source record; caller must still re-resolve it before use."""
    try:
        source = Source(
            id=str(record["id"]),
            selector=str(record["selector"]),
            root=Path(record["root"]),
            relative=str(record["relative"]),
            display_name=str(record["display_name"]),
            discovery_context=str(record["discovery_context"]),
            latest_snapshot=record.get("latest_snapshot"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LayerError("INVALID_SOURCE", f"Malformed stored source record: {exc}") from exc
    if not source.id or not source.selector or not source.root.is_absolute() or not source.relative:
        raise LayerError("INVALID_SOURCE", "Stored source record is missing a verified identity.")
    from .snapshots import validate_path

    validate_path(source.relative)
    if source.latest_snapshot is not None and not isinstance(source.latest_snapshot, str):
        raise LayerError("INVALID_SOURCE", "Stored source snapshot reference is malformed.")
    return source


class Discovery:
    """A single profile/discovery context and its verified selector set."""

    def __init__(self, scope: ProfileScope):
        self.scope = scope

    @staticmethod
    def _host_helpers() -> tuple[
        Callable[[], list[dict[str, Any]]],
        Callable[
            [str, str | None, list[tuple[int, Path]]], tuple[str | None, Path | None, Path | None]
        ],
    ]:
        try:
            from tools.skills_tool import find_all_skills, locate_skill
        except Exception as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Hermes skill discovery helpers are unavailable: {exc}"
            ) from exc
        return find_all_skills, locate_skill

    def _locate(self, selector: str) -> tuple[Path, Path]:
        find_all_skills, locate_skill = self._host_helpers()
        selectors = {item.get("name") for item in find_all_skills()}
        if selector not in selectors:
            raise LayerError(
                "SOURCE_MOVED", "The saved skill selector is no longer discoverable.", True
            )
        error_json, skill_dir, skill_md = locate_skill(selector, None, list(self.scope.roots))
        if error_json is not None or skill_dir is None or skill_md is None:
            raise LayerError(
                "SOURCE_MOVED",
                f"Skill selector {selector!r} no longer resolves uniquely.",
                True,
            )
        raw_dir = Path(skill_dir)
        raw_md = Path(skill_md)
        try:
            raw_absolute = Path(os.path.abspath(raw_dir))
            candidates = [
                root
                for _tier, root in self.scope.roots
                if raw_absolute == root or raw_absolute.is_relative_to(root)
            ]
            if not candidates:
                raise LayerError(
                    "PATH_UNSAFE", f"Hermes resolved an out-of-root skill for {selector!r}."
                )
            matched_root = max(candidates, key=lambda root: len(root.parts))
            cursor = matched_root
            for part in raw_absolute.relative_to(matched_root).parts:
                cursor = cursor / part
                info = cursor.lstat()
                if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                    raise LayerError(
                        "PATH_UNSAFE", f"Hermes resolved a linked skill path for {selector!r}."
                    )
            md_info = raw_md.lstat()
            if stat.S_ISLNK(md_info.st_mode) or _is_reparse_point(md_info):
                raise LayerError(
                    "PATH_UNSAFE", f"Hermes resolved a linked SKILL.md for {selector!r}."
                )
        except OSError as exc:
            raise LayerError(
                "SOURCE_MOVED", f"Skill selector {selector!r} changed during resolution.", True
            ) from exc
        resolved_dir = _canonical(raw_dir)
        resolved_md = _canonical(raw_md)
        if resolved_md.parent != resolved_dir or resolved_md.name != "SKILL.md":
            raise LayerError(
                "PATH_UNSAFE", f"Hermes resolved an unsafe skill path for {selector!r}."
            )
        if not resolved_md.is_file() or not resolved_dir.is_dir():
            raise LayerError("SOURCE_MOVED", f"Skill selector {selector!r} no longer exists.", True)
        return resolved_dir, resolved_md

    def _root_and_relative(self, directory: Path) -> tuple[Path, str]:
        matches = [
            (root, directory.relative_to(root))
            for _tier, root in self.scope.roots
            if directory == root or directory.is_relative_to(root)
        ]
        if not matches:
            raise LayerError("PATH_UNSAFE", "Resolved skill is outside its discovery roots.")
        root, relative = max(matches, key=lambda pair: len(pair[0].parts))
        if not relative.parts:
            raise LayerError("PATH_UNSAFE", "A skill must be contained below a discovery root.")
        return root, relative.as_posix()

    def _make_source(self, selector: str, directory: Path) -> Source:
        root, relative = self._root_and_relative(directory)
        try:
            (directory / MARKER).lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise LayerError(
                "UNSUPPORTED_SOURCE", "Could not verify reserved export metadata."
            ) from exc
        else:
            raise LayerError(
                "UNSUPPORTED_SOURCE", "Managed export trees cannot be selected as sources."
            )
        source_id = self._source_id(root, relative)
        return Source(source_id, selector, root, relative, selector, self.scope.discovery_context)

    def _source_id(self, root: Path, relative: str) -> str:
        return "source-" + _digest([self.scope.discovery_context, str(root), relative])[:40]

    def sources(self) -> list[Source]:
        """Enumerate only selectors Hermes exposes and successfully resolves on disk."""
        find_all_skills, _locate = self._host_helpers()
        result: list[Source] = []
        seen: set[tuple[str, str]] = set()
        for item in find_all_skills():
            selector = item.get("name")
            if not isinstance(selector, str) or not selector:
                continue
            try:
                directory, _skill_md = self._locate(selector)
                source = self._make_source(selector, directory)
            except LayerError as exc:
                # Discovery can race a removal or contain an unsupported entry. Such a row is
                # not selectable; resolution of a previously stored row reports SOURCE_MOVED.
                if exc.code in {"SOURCE_MOVED", "PATH_UNSAFE", "UNSUPPORTED_SOURCE"}:
                    continue
                raise
            key = (source.selector, str(directory))
            if key not in seen:
                seen.add(key)
                result.append(source)
        return sorted(
            result, key=lambda source: (source.display_name.casefold(), source.selector, source.id)
        )

    def lookup(self, source_id: str) -> Source:
        """Resolve an opaque ID only from the current verified selector set."""
        if not isinstance(source_id, str) or not source_id.startswith("source-"):
            raise LayerError(
                "INVALID_SOURCE", "Source IDs must come from the current discovered source list."
            )
        for source in self.sources():
            if source.id == source_id:
                return source
        raise LayerError("INVALID_SOURCE", "Unknown source ID in the current discovery context.")

    def resolve(self, source: Source) -> Path:
        """Re-resolve a stored selector and reject any root/location rebinding."""
        if not isinstance(source, Source):
            raise LayerError("INVALID_SOURCE", "Resolve requires a verified Source record.")
        if source.discovery_context != self.scope.discovery_context:
            raise LayerError(
                "SOURCE_MOVED", "Source discovery context changed; select the source again.", True
            )
        from .snapshots import validate_path

        relative = validate_path(source.relative)
        if source.id != self._source_id(_canonical(source.root), relative):
            raise LayerError(
                "INVALID_SOURCE", "Stored source identity does not match its discovery record."
            )
        find_all_skills, _locate = self._host_helpers()
        selectors: list[str] = []
        for item in find_all_skills():
            selector = item.get("name")
            if isinstance(selector, str) and selector:
                selectors.append(selector)
        if source.selector in selectors:
            directory, _skill_md = self._locate(source.selector)
            current = self._make_source(source.selector, directory)
            if current.root != source.root or current.relative != source.relative:
                raise LayerError(
                    "SOURCE_MOVED",
                    "The saved selector now resolves to a different installed location.",
                    True,
                    {
                        "selector": source.selector,
                        "previous_relative": source.relative,
                        "current_relative": current.relative,
                    },
                )
            return directory

        # A frontmatter rename changes Hermes' selector but not source location identity.
        # Rebind only through currently discovered selectors resolving to the exact recorded
        # root and relative path; never pass a stored path to the host lookup helper.
        for selector in selectors:
            try:
                directory, _skill_md = self._locate(selector)
                current = self._make_source(selector, directory)
            except LayerError as exc:
                if exc.code in {"SOURCE_MOVED", "PATH_UNSAFE", "UNSUPPORTED_SOURCE"}:
                    continue
                raise
            if current.root == source.root and current.relative == source.relative:
                return directory
        else:
            raise LayerError(
                "SOURCE_MOVED",
                "The saved source location is no longer among the discovered skills.",
                True,
                {
                    "selector": source.selector,
                    "previous_relative": source.relative,
                },
            )

    def verify_export(self, selector: str, directory: Path) -> None:
        """Confirm a just-published directory is discoverable by its exact native selector."""
        try:
            from tools.skills_tool import find_all_skills, locate_skill, skill_search_dirs

            roots_raw, active_raw = skill_search_dirs()
            active = _canonical(Path(active_raw))
            roots = tuple((int(tier), _canonical(Path(root))) for tier, root in roots_raw)
            expected = _canonical(Path(directory))
            if active != self.scope.skills_dir or expected.parent != active:
                raise LayerError(
                    "RECOVERY_REQUIRED",
                    "Published export is outside the active profile skills directory.",
                )
            if not any(item.get("name") == selector for item in find_all_skills()):
                raise LayerError(
                    "RECOVERY_REQUIRED",
                    "Published export is not visible to Hermes skill discovery.",
                )
            error_json, skill_dir, skill_md = locate_skill(selector, None, list(roots))
            if error_json is not None or skill_dir is None or skill_md is None:
                raise LayerError(
                    "RECOVERY_REQUIRED", "Published export selector does not resolve uniquely."
                )
            resolved_dir = _canonical(Path(skill_dir))
            resolved_md = _canonical(Path(skill_md))
            raw_dir = Path(skill_dir)
            candidates = [
                root
                for _tier, root in roots
                if Path(os.path.abspath(raw_dir)) == root
                or Path(os.path.abspath(raw_dir)).is_relative_to(root)
            ]
            if not candidates:
                raise LayerError(
                    "RECOVERY_REQUIRED",
                    "Published export resolved outside the active discovery roots.",
                )
            matched_root = max(candidates, key=lambda root: len(root.parts))
            cursor = matched_root
            for part in Path(os.path.abspath(raw_dir)).relative_to(matched_root).parts:
                cursor = cursor / part
                info = cursor.lstat()
                if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                    raise LayerError(
                        "RECOVERY_REQUIRED", "Published export resolved through a linked path."
                    )
            md_info = Path(skill_md).lstat()
            if stat.S_ISLNK(md_info.st_mode) or _is_reparse_point(md_info):
                raise LayerError(
                    "RECOVERY_REQUIRED", "Published SKILL.md resolved through a linked path."
                )
            if resolved_dir != expected or resolved_md != expected / "SKILL.md":
                raise LayerError(
                    "RECOVERY_REQUIRED",
                    "Published selector resolved to a different skill directory.",
                )
        except LayerError:
            raise
        except Exception as exc:
            raise LayerError(
                "RECOVERY_REQUIRED",
                f"Could not verify published export discovery: {type(exc).__name__}.",
            ) from exc


def _is_reparse_point(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)
