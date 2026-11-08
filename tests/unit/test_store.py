import hashlib
import os
import sqlite3
import sys
import types

import pytest

from skill_layers.contracts import LayerError, ProfileScope, TreeFile
from skill_layers.snapshots import make_snapshot
from skill_layers.store import Store


def _scope(home, skills=None, profile_id="profile-test"):
    return ProfileScope(profile_id, home, skills or home / "skills", (), "discovery-test")


def _plugin_storage(monkeypatch, home):
    package = types.ModuleType("plugins")
    package.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    storage.plugin_data_dir = plugin_data_dir
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "plugins", package)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)


def _initialized_store(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    scope = _scope(home)
    _plugin_storage(monkeypatch, home)
    store = Store(scope)
    store.initialize()
    return scope, store


def test_readonly_absent_store_has_no_filesystem_side_effect(tmp_path):
    home = tmp_path / "absent-home"
    store = Store(_scope(home), readonly=True)
    assert store.root == home / "plugin-data" / "skill-layers"
    assert store.list("layers") == []
    assert not home.exists()
    with pytest.raises(LayerError) as error:
        store.get("layers", "missing")
    assert error.value.code == "NOT_FOUND"
    assert not home.exists()


def test_initialize_uses_delete_journal_and_binds_profile(tmp_path, monkeypatch):
    scope, store = _initialized_store(tmp_path, monkeypatch)
    assert store.database.is_file()
    if os.name != "nt":
        assert store.root.stat().st_mode & 0o777 == 0o700
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    rows = {
        row["key"]: row["value"] for row in store._conn.execute("SELECT key,value FROM metadata")
    }
    assert rows == {
        "profile_id": scope.id,
        "home": str(scope.home),
        "skills_dir": str(scope.skills_dir),
    }
    store.close()


def test_readonly_store_does_not_initialize_or_create_wal_sidecars(tmp_path, monkeypatch):
    scope, store = _initialized_store(tmp_path, monkeypatch)
    store.put("layers", "layer-a", {"id": "layer-a", "revision": 1})
    store.close()
    before = {path.name for path in store.root.iterdir()}
    readonly = Store(scope, readonly=True)
    assert readonly.get("layers", "layer-a") == {"id": "layer-a", "revision": 1}
    assert readonly.list("layers") == [{"id": "layer-a", "revision": 1}]
    readonly.close()
    after = {path.name for path in store.root.iterdir()}
    assert after == before
    assert "data.db-wal" not in after
    assert "data.db-shm" not in after


def test_records_are_transactional_and_nested_helpers_share_outer_tx(tmp_path, monkeypatch):
    _scope_value, store = _initialized_store(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("layers", "layer-a", {"id": "layer-a"})
            with store.transaction():
                store.put("operations", "op-a", {"id": "op-a"})
            raise RuntimeError("inject rollback")
    assert store.list("layers") == []
    assert store.list("operations") == []
    store.put("layers", "layer-b", {"id": "layer-b", "revision": 3})
    assert store.get("layers", "layer-b")["revision"] == 3
    with pytest.raises(LayerError) as error:
        store.get("layers", "absent")
    assert error.value.code == "NOT_FOUND"
    store.close()


def test_transaction_rolls_back_on_base_exception_like_interrupted_process(tmp_path, monkeypatch):
    _scope_value, store = _initialized_store(tmp_path, monkeypatch)

    class SimulatedInterrupt(BaseException):
        pass

    with pytest.raises(SimulatedInterrupt):
        with store.transaction():
            store.put("operations", "pending", {"id": "pending", "phase": "started"})
            raise SimulatedInterrupt()
    assert store.list("operations") == []
    store.put("operations", "recovered", {"id": "recovered", "phase": "retry"})
    assert store.get("operations", "recovered")["phase"] == "retry"
    store.close()


def test_snapshot_content_objects_are_hash_verified_and_round_trip(tmp_path, monkeypatch):
    _scope_value, store = _initialized_store(tmp_path, monkeypatch)
    tree = {
        "SKILL.md": TreeFile(b"---\nname: demo\n---\nbody\n"),
        "bin/run": TreeFile(b"x", True, 0o100),
    }
    snapshot = make_snapshot(tree, "raw", {"name": "demo"})
    store.save_snapshot(snapshot)
    loaded = store.load_snapshot(snapshot.digest)
    assert loaded == snapshot
    content_hash = hashlib.sha256(b"x").hexdigest()
    object_path = store.objects / content_hash[:2] / content_hash[2:]
    assert object_path.read_bytes() == b"x"
    store.close()


def test_snapshot_object_corruption_fails_closed(tmp_path, monkeypatch):
    _scope_value, store = _initialized_store(tmp_path, monkeypatch)
    snapshot = make_snapshot({"SKILL.md": TreeFile(b"skill")}, "raw")
    store.save_snapshot(snapshot)
    content_hash = hashlib.sha256(b"skill").hexdigest()
    (store.objects / content_hash[:2] / content_hash[2:]).write_bytes(b"tampered")
    with pytest.raises(LayerError) as error:
        store.load_snapshot(snapshot.digest)
    assert error.value.code == "INTEGRITY_ERROR"
    store.close()


def test_store_refuses_cross_profile_database_copy(tmp_path, monkeypatch):
    scope, store = _initialized_store(tmp_path, monkeypatch)
    store.close()
    wrong_scope = _scope(scope.home, scope.skills_dir, profile_id="profile-other")
    with pytest.raises(LayerError) as error:
        Store(wrong_scope)
    assert error.value.code == "PROFILE_MISMATCH"


def test_store_rejects_symlinked_plugin_data_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "plugin-data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LayerError) as error:
        Store(_scope(home), readonly=True)
    assert error.value.code == "PATH_UNSAFE"


def test_readonly_store_rejects_write_and_initialize(tmp_path, monkeypatch):
    scope, writer = _initialized_store(tmp_path, monkeypatch)
    writer.close()
    reader = Store(scope, readonly=True)
    with pytest.raises(LayerError):
        reader.initialize()
    with pytest.raises(LayerError):
        reader.put("layers", "nope", {"id": "nope"})
    reader.close()


def test_explicit_schema_migration_backs_up_existing_database(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    _plugin_storage(monkeypatch, home)
    root = home / "plugin-data" / "skill-layers"
    root.mkdir(parents=True)
    database = root / "data.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO legacy_state VALUES ('preserve')")
    connection.commit()
    connection.close()

    store = Store(_scope(home))
    with pytest.raises(LayerError) as error:
        store.list("layers")
    assert error.value.code == "STORAGE_ERROR"
    store.initialize()
    store.close()

    backups = list(root.glob("data.db.schema-backup-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("SELECT value FROM legacy_state").fetchone()[0] == "preserve"
    finally:
        backup.close()
    migrated = sqlite3.connect(database)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 1
        assert migrated.execute("SELECT value FROM legacy_state").fetchone()[0] == "preserve"
    finally:
        migrated.close()


def test_content_object_crash_after_atomic_move_remains_readable(tmp_path, monkeypatch):
    from skill_layers import publish

    _, store = _initialized_store(tmp_path, monkeypatch)
    content = b"Durable object after rename"
    digest = hashlib.sha256(content).hexdigest()
    original = publish.rename_new

    class Crash(BaseException):
        pass

    def move_then_crash(source, target, guard):
        original(source, target, guard)
        raise Crash()

    monkeypatch.setattr(publish, "rename_new", move_then_crash)
    with pytest.raises(Crash):
        store._write_object(content, digest)
    path = store._object_path(digest)
    assert path.stat().st_nlink == 1
    assert store._verify_object(path, digest) == content
    monkeypatch.setattr(publish, "rename_new", original)
    store._write_object(content, digest)
    assert list(path.parent.glob(".object-*")) == []
    store.close()
