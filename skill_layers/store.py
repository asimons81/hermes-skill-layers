"""Profile-bound, explicit-initialization SQLite and immutable snapshot object store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .contracts import SCHEMA_VERSION, LayerError, ProfileScope, Snapshot, Tree, TreeFile
from .snapshots import verify_snapshot

_KINDS = {"sources", "layers", "exports", "checkpoints", "candidates", "operations"}


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LayerError("STORAGE_ERROR", f"Record is not canonical JSON: {exc}") from exc


class Store:
    """Explicitly initialized profile storage; read methods never create files or schemas."""

    def __init__(self, scope: ProfileScope, *, readonly: bool = False):
        self.scope = scope
        self.readonly = readonly
        self.root = Path(scope.home).resolve(strict=False) / "plugin-data" / "skill-layers"
        self.database = self.root / "data.db"
        self.objects = self.root / "objects"
        self._conn: sqlite3.Connection | None = None
        self._initialized = False
        self._transaction_depth = 0
        self._validate_storage_paths()
        if self.database.exists():
            self._conn = self._open_existing()
            self._validate_journal_mode()
            version = self._validate_schema_version()
            if version == SCHEMA_VERSION:
                self._validate_binding()
                self._initialized = True
            elif readonly:
                raise LayerError(
                    "STORAGE_ERROR",
                    f"Read-only store schema {version} is unsupported; initialize it explicitly.",
                )

    def _validate_storage_paths(self) -> None:
        for path in (self.root.parent, self.root, self.database):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise LayerError(
                    "STORAGE_ERROR", f"Cannot inspect plugin storage path {path}: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise LayerError(
                    "PATH_UNSAFE", f"Plugin storage path may not be a symlink: {path}."
                )
            if path == self.database and info.st_nlink != 1:
                raise LayerError("PATH_UNSAFE", "Plugin database may not be hard-linked.")
        if self.root.exists() and not self.root.is_dir():
            raise LayerError("PATH_UNSAFE", "Plugin data root must be a directory.")
        if self.database.exists() and not self.database.is_file():
            raise LayerError("PATH_UNSAFE", "Plugin database must be a regular file.")

    def _open_existing(self) -> sqlite3.Connection:
        if self.readonly:
            uri_path = quote(self.database.as_posix(), safe="/")
            uri = f"file:{uri_path}?mode=ro"
            try:
                connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
            except sqlite3.Error as exc:
                raise LayerError(
                    "STORAGE_ERROR", f"Could not open plugin state read-only: {exc}"
                ) from exc
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection
        try:
            connection = sqlite3.connect(self.database, isolation_level=None, timeout=5.0)
        except sqlite3.Error as exc:
            raise LayerError("STORAGE_ERROR", f"Could not open plugin state: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _connection(self, *, write: bool = False) -> sqlite3.Connection | None:
        if write and self.readonly:
            raise LayerError("STORAGE_ERROR", "Read-only storage cannot be modified.")
        self._validate_storage_paths()
        if self._conn is None and self.database.exists():
            self._conn = self._open_existing()
            self._validate_journal_mode()
            version = self._validate_schema_version()
            if version != SCHEMA_VERSION:
                raise LayerError(
                    "STORAGE_ERROR", "Plugin state requires explicit initialization or migration."
                )
            self._validate_binding()
            self._initialized = True
        if self._conn is None:
            if write:
                raise LayerError(
                    "STORAGE_ERROR",
                    "Plugin storage is not initialized; call initialize() explicitly.",
                )
            return None
        if not self._initialized:
            raise LayerError(
                "STORAGE_ERROR", "Plugin state requires explicit initialization or migration."
            )
        return self._conn

    def _validate_binding(self) -> None:
        connection = self._conn
        if connection is None:
            return
        try:
            rows = connection.execute(
                "SELECT key, value FROM metadata WHERE key IN ('profile_id','home','skills_dir')"
            ).fetchall()
        except sqlite3.Error as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Plugin database is not initialized or readable: {exc}"
            ) from exc
        values = {row["key"]: row["value"] for row in rows}
        expected = {
            "profile_id": self.scope.id,
            "home": str(Path(self.scope.home).resolve(strict=False)),
            "skills_dir": str(Path(self.scope.skills_dir).resolve(strict=False)),
        }
        if values != expected:
            raise LayerError(
                "PROFILE_MISMATCH",
                "Plugin state belongs to a different canonical Hermes profile.",
                details={"expected_profile_id": self.scope.id},
            )

    def _validate_journal_mode(self) -> None:
        if self._conn is None:
            return
        try:
            mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Could not inspect SQLite journal mode: {exc}"
            ) from exc
        if str(mode).lower() != "delete":
            raise LayerError(
                "STORAGE_ERROR",
                f"Unsupported SQLite journal mode {mode!r}; DELETE mode is required.",
            )

    def _validate_schema_version(self) -> int:
        if self._conn is None:
            return 0
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Could not inspect SQLite schema version: {exc}"
            ) from exc
        if version > SCHEMA_VERSION:
            raise LayerError(
                "STORAGE_ERROR",
                f"Plugin schema version {version} is newer than supported {SCHEMA_VERSION}.",
            )
        return int(version)

    def initialize(self) -> None:
        """Create or explicitly migrate the store; never called implicitly by reads."""
        if self.readonly:
            raise LayerError(
                "STORAGE_ERROR", "Read-only storage cannot be initialized or migrated."
            )
        if self._initialized:
            return
        self._validate_storage_paths()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        try:
            from hermes_constants import get_hermes_home
            from plugins.plugin_storage import plugin_data_dir

            active_home = Path(get_hermes_home()).resolve(strict=False)
            if active_home != Path(self.scope.home).resolve(strict=False):
                raise LayerError(
                    "PROFILE_MISMATCH", "The active Hermes profile changed before initialization."
                )
            host_root = Path(plugin_data_dir("skill-layers"))
        except Exception as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Hermes plugin storage helper is unavailable: {exc}"
            ) from exc
        expected_root = Path(self.scope.home) / "plugin-data" / "skill-layers"
        if host_root.resolve(strict=False) != expected_root.resolve(strict=False):
            raise LayerError(
                "PROFILE_MISMATCH", "Plugin storage resolved outside the bound profile."
            )
        self.root = expected_root
        self.database = self.root / "data.db"
        self.objects = self.root / "objects"
        database_existed = self.database.exists()
        try:
            info = self.root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise LayerError("PATH_UNSAFE", "Plugin data root must be a real directory.")
            self.root.chmod(0o700)
            connection = sqlite3.connect(self.database, isolation_level=None, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                connection.close()
                raise LayerError(
                    "STORAGE_ERROR",
                    f"Plugin schema version {version} is newer than supported {SCHEMA_VERSION}.",
                )
            if version not in (0, SCHEMA_VERSION):
                connection.close()
                raise LayerError(
                    "STORAGE_ERROR", f"No migration path from plugin schema {version}."
                )
            if database_existed and version < SCHEMA_VERSION:
                self._backup_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(kind,id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "digest TEXT PRIMARY KEY, representation TEXT NOT NULL, manifest TEXT NOT NULL)"
            )
            expected = {
                "profile_id": self.scope.id,
                "home": str(Path(self.scope.home).resolve(strict=False)),
                "skills_dir": str(Path(self.scope.skills_dir).resolve(strict=False)),
            }
            existing = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('profile_id','home','skills_dir')"
                )
            }
            if existing and existing != expected:
                connection.rollback()
                connection.close()
                raise LayerError(
                    "PROFILE_MISMATCH",
                    "Plugin state belongs to a different canonical Hermes profile.",
                )
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", expected.items()
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
            self._conn = connection
            self._initialized = True
            self._validate_binding()
            self._validate_journal_mode()
            self._validate_schema_version()
            self._fsync_path(self.database)
            self._fsync_dir(self.root)
        except LayerError:
            raise
        except sqlite3.Error as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Could not initialize plugin database: {exc}"
            ) from exc
        except OSError as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Could not initialize plugin storage: {exc}"
            ) from exc

    @staticmethod
    def _backup_database(connection: sqlite3.Connection) -> None:
        database_path = connection.execute("PRAGMA database_list").fetchone()[2]
        if not database_path:
            raise LayerError("STORAGE_ERROR", "Cannot locate database for migration backup.")
        source = Path(database_path)
        destination = source.with_name(f"{source.name}.schema-backup-{time.time_ns()}")
        try:
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            backup = sqlite3.connect(destination)
            try:
                connection.backup(backup)
            finally:
                backup.close()
            Store._fsync_path(destination)
            Store._fsync_dir(destination.parent)
        except sqlite3.Error as exc:
            destination.unlink(missing_ok=True)
            raise LayerError(
                "STORAGE_ERROR", "Could not back up plugin state before migration."
            ) from exc
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise LayerError("STORAGE_ERROR", "Could not persist plugin migration backup.") from exc

    @staticmethod
    def _fsync_path(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # SQLite already requested FULL synchronous durability; some platforms cannot
            # fsync a file descriptor opened read-only.
            pass

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._transaction_depth = 0

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Use one outer transaction; nested service helpers participate in it."""
        connection = self._connection(write=True)
        assert connection is not None
        if self._transaction_depth:
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
            return
        self._transaction_depth = 1
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._transaction_depth = 0

    def _check_kind(self, kind: str) -> None:
        if kind not in _KINDS:
            raise LayerError("STORAGE_ERROR", f"Unknown record kind {kind!r}.")

    def get(self, kind: str, record_id: str) -> dict[str, Any]:
        self._check_kind(kind)
        connection = self._connection()
        if connection is None:
            raise LayerError("NOT_FOUND", f"No {kind} record {record_id!r} exists.")
        row = connection.execute(
            "SELECT payload FROM records WHERE kind=? AND id=?", (kind, record_id)
        ).fetchone()
        if row is None:
            raise LayerError("NOT_FOUND", f"No {kind} record {record_id!r} exists.")
        try:
            value = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Stored {kind} record {record_id!r} is corrupt: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise LayerError(
                "STORAGE_ERROR", f"Stored {kind} record {record_id!r} is not an object."
            )
        return value

    def list(self, kind: str) -> list[dict[str, Any]]:
        self._check_kind(kind)
        connection = self._connection()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                "SELECT payload FROM records WHERE kind=? ORDER BY id", (kind,)
            ).fetchall()
            values = [json.loads(row["payload"]) for row in rows]
        except (sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
            raise LayerError("STORAGE_ERROR", f"Could not read {kind} records: {exc}") from exc
        if any(not isinstance(value, dict) for value in values):
            raise LayerError("STORAGE_ERROR", f"Stored {kind} records must be objects.")
        return values

    def put(self, kind: str, record_id: str, record: dict[str, Any]) -> None:
        self._check_kind(kind)
        if not isinstance(record_id, str) or not record_id or not isinstance(record, dict):
            raise LayerError("STORAGE_ERROR", "Records require a non-empty ID and object payload.")
        payload = _json(record)
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO records(kind,id,payload) VALUES(?,?,?)",
                (kind, record_id, payload),
            )

    @contextlib.contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection(write=True)
        assert connection is not None
        if self._transaction_depth:
            yield connection
        else:
            with self.transaction():
                yield connection

    def _object_path(self, digest: str) -> Path:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise LayerError("INTEGRITY_ERROR", "Invalid content object digest.")
        return self.objects / digest[:2] / digest[2:]

    def _write_object(self, content: bytes, digest: str) -> None:
        target = self._object_path(digest)
        try:
            self.objects.mkdir(mode=0o700)
        except FileExistsError:
            pass
        objects_info = self.objects.lstat()
        if stat.S_ISLNK(objects_info.st_mode) or not stat.S_ISDIR(objects_info.st_mode):
            raise LayerError("PATH_UNSAFE", "Content object root must be a real directory.")
        try:
            target.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        parent_info = target.parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise LayerError("PATH_UNSAFE", "Content object directory must be a real directory.")
        if target.exists():
            self._verify_object(target, digest)
            return
        fd, temporary_name = tempfile.mkstemp(prefix=".object-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                self._verify_object(target, digest)
            temporary.unlink()
            self._fsync_dir(target.parent)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LayerError(
                "STORAGE_ERROR", f"Could not persist immutable content object: {exc}"
            ) from exc

    @staticmethod
    def _verify_object(path: Path, digest: str) -> bytes:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LayerError(
                    "INTEGRITY_ERROR", f"Content object is not a regular file: {path}."
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_nlink,
                ):
                    raise LayerError("INTEGRITY_ERROR", "Content object changed while opening.")
                content = stream.read()
                after = os.fstat(stream.fileno())
            after_path = path.lstat()
        except OSError as exc:
            raise LayerError(
                "STORAGE_ERROR", f"Could not read content object {path}: {exc}"
            ) from exc
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (after.st_dev, after.st_ino) != (after_path.st_dev, after_path.st_ino):
            raise LayerError("INTEGRITY_ERROR", "Content object changed while reading.")
        if hashlib.sha256(content).hexdigest() != digest:
            raise LayerError("INTEGRITY_ERROR", f"Content object hash mismatch: {path}.")
        return content

    def save_snapshot(self, snapshot: Snapshot) -> None:
        verify_snapshot(snapshot)
        self._connection(write=True)
        if len(snapshot.digest) != 64 or any(
            char not in "0123456789abcdef" for char in snapshot.digest
        ):
            raise LayerError("INTEGRITY_ERROR", "Invalid snapshot digest.")
        # Objects are immutable and can safely remain orphaned if the following SQL write fails.
        for entry in snapshot.manifest["files"]:
            content = snapshot.tree[entry["path"]].content
            self._write_object(content, entry["sha256"])
        manifest_text = _json(snapshot.manifest)
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT representation,manifest FROM snapshots WHERE digest=?", (snapshot.digest,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["representation"] != snapshot.representation
                    or existing["manifest"] != manifest_text
                ):
                    raise LayerError(
                        "INTEGRITY_ERROR", "Snapshot digest already exists with different content."
                    )
                return
            connection.execute(
                "INSERT INTO snapshots(digest,representation,manifest) VALUES(?,?,?)",
                (snapshot.digest, snapshot.representation, manifest_text),
            )

    def load_snapshot(self, digest: str) -> Snapshot:
        connection = self._connection()
        if connection is None:
            raise LayerError("NOT_FOUND", f"No snapshot {digest!r} exists.")
        row = connection.execute(
            "SELECT representation,manifest FROM snapshots WHERE digest=?", (digest,)
        ).fetchone()
        if row is None:
            raise LayerError("NOT_FOUND", f"No snapshot {digest!r} exists.")
        try:
            manifest = json.loads(row["manifest"])
            tree: Tree = {}
            for entry in manifest["files"]:
                path = entry["path"]
                content = self._verify_object(self._object_path(entry["sha256"]), entry["sha256"])
                if len(content) != entry["size"]:
                    raise LayerError(
                        "INTEGRITY_ERROR", f"Content object size mismatch for {path!r}."
                    )
                tree[path] = TreeFile(content, bool(entry["executable"]), entry["execute_bits"])
            snapshot = Snapshot(digest, row["representation"], manifest, tree)
            verify_snapshot(snapshot)
        except LayerError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LayerError(
                "INTEGRITY_ERROR", f"Stored snapshot {digest!r} is malformed: {exc}"
            ) from exc
        if snapshot.digest != digest:
            raise LayerError(
                "INTEGRITY_ERROR", f"Snapshot key {digest!r} does not match its manifest."
            )
        return snapshot
