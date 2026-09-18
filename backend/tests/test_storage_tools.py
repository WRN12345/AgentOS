"""Operational tooling tests use only fake DB sessions and isolated storage."""

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.infrastructure.storage.local import LocalStorageProvider
from app.scripts.migrate_file_storage import FileRecord, copy_verified, migrate
from app.scripts.storage_snapshot import export_snapshot, import_snapshot, read_manifest, verify_snapshot


# Override the integration-suite fixtures: no PostgreSQL, Redis or prod mutations.
@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database():
    pass


@pytest.fixture(autouse=True)
def _clean_tables():
    pass


class FakeProvider:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.commits = []
        self.discards = 0
        self.stages = 0
        self.corrupt_commit = False
        self.fail_commit = False

    async def exists(self, key):
        return key in self.objects

    async def iter_chunks(self, key):
        data = self.objects[key]
        for offset in range(0, len(data), 3):
            yield data[offset:offset + 3]

    async def stage(self):
        self.stages += 1
        chunks = []

        async def write(chunk):
            chunks.append(chunk)

        return SimpleNamespace(write=write, chunks=chunks)

    async def commit(self, staged, key):
        self.commits.append(key)
        self.objects[key] = b"bad" if self.corrupt_commit else b"".join(staged.chunks)
        if self.fail_commit:
            raise RuntimeError("Commit response lost")

    async def discard(self, staged):
        self.discards += 1


def row(key="one/file.txt", data=b"hello storage", backend="local", **extra):
    return SimpleNamespace(
        id=uuid.uuid4(), storage_key=key, storage_backend=backend,
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), **extra,
    )


class FakeDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.lock_available = True
        self.fail_db_commit = False
        self.lock_queries = 0
        self.row_locks = 0
        self.updates = 0

    @asynccontextmanager
    async def session(self):
        database = self

        class Session:
            changed = False

            @asynccontextmanager
            async def begin(self):
                original = [item.storage_backend for item in database.rows]
                try:
                    yield
                except BaseException:
                    for item, backend in zip(database.rows, original):
                        item.storage_backend = backend
                    raise
                if self.changed and database.fail_db_commit:
                    # Simulate commit succeeding but the response being lost.
                    raise RuntimeError("Database commit response lost")

            async def scalar(self, statement, params=None):
                if params is not None:
                    assert "pg_try_advisory_xact_lock" in str(statement)
                    database.lock_queries += 1
                    return database.lock_available
                assert statement._for_update_arg is not None
                database.row_locks += 1
                source = statement.column_descriptions[0]["entity"].__tablename__
                return next(item for item in database.rows
                            if item.id == statement.compile().params["id_1"]
                            and getattr(item, "source", "stored_files") == source)

            async def scalars(self, statement):
                params = statement.compile().params
                source = statement.column_descriptions[0]["entity"].__tablename__
                selected = [item for item in database.rows
                            if getattr(item, "source", "stored_files") == source
                            and ("storage_backend_1" not in params or item.storage_backend == params["storage_backend_1"])]
                if statement.column_descriptions[0]["name"] == "id":
                    selected = [item.id for item in selected]
                return SimpleNamespace(all=lambda: selected)

            async def execute(self, statement):
                params = statement.compile().params
                selected = next(item for item in database.rows if item.id == params["id_1"]
                                and getattr(item, "source", "stored_files") == statement.table.name)
                selected.storage_backend = params["storage_backend"]
                self.changed = True
                database.updates += 1

        yield Session()


async def test_migration_dry_run_then_all_versions_and_resume():
    rows = [row(superseded_by=uuid.uuid4()), row("two/file.txt", superseded_by=None)]
    db = FakeDatabase(rows)
    source = FakeProvider({item.storage_key: b"hello storage" for item in rows})
    target = FakeProvider({rows[0].storage_key: b"hello storage"})
    providers = {"local": source, "minio": target}
    assert await migrate("local", "minio", session_factory=db.session, provider_for=providers.__getitem__) == 0
    assert db.updates == target.stages == 0
    assert await migrate("local", "minio", apply=True, session_factory=db.session, provider_for=providers.__getitem__) == 0
    assert all(item.storage_backend == "minio" for item in rows)
    assert target.commits == [rows[1].storage_key]
    assert target.objects == source.objects
    assert db.row_locks == 4
    assert await migrate("local", "minio", apply=True, session_factory=db.session, provider_for=providers.__getitem__) == 0
    assert len(target.commits) == 1


@pytest.mark.parametrize("failure", ["source", "destination", "published", "commit", "db_commit"])
async def test_migration_failures_retain_sources_and_published_objects(failure):
    item = row()
    db = FakeDatabase([item])
    source = FakeProvider({item.storage_key: b"bad" if failure == "source" else b"hello storage"})
    target = FakeProvider({item.storage_key: b"existing mismatch"} if failure == "destination" else {})
    target.corrupt_commit = failure == "published"
    target.fail_commit = failure == "commit"
    db.fail_db_commit = failure == "db_commit"
    providers = {"local": source, "minio": target}
    assert await migrate("local", "minio", apply=True, session_factory=db.session, provider_for=providers.__getitem__) == 1
    assert item.storage_key in source.objects
    if failure != "db_commit":
        assert item.storage_backend == "local"
    if failure == "source":
        assert not target.objects and not target.commits
    else:
        assert item.storage_key in target.objects
    if failure == "destination":
        assert target.objects[item.storage_key] == b"existing mismatch"
        assert target.stages == 0


async def test_operation_lock_blocks_reverse_migration_and_snapshot(tmp_path):
    db = FakeDatabase([])
    db.lock_available = False
    with pytest.raises(RuntimeError, match="Another storage"):
        await migrate("minio", "local", apply=True, session_factory=db.session)
    with pytest.raises(RuntimeError, match="Another storage"):
        await export_snapshot(tmp_path, session_factory=db.session)
    assert db.lock_queries == 2


async def test_snapshot_mixed_backend_export_and_isolated_restore(tmp_path):
    rows = [row(), row("two/file.txt", backend="minio"), row("extra/file.txt")]
    db = FakeDatabase(rows)
    original = {
        "local": FakeProvider({item.storage_key: b"hello storage" for item in rows if item.storage_backend == "local"}),
        "minio": FakeProvider({rows[1].storage_key: b"hello storage"}),
    }
    snapshot = tmp_path / "snapshot"
    await export_snapshot(snapshot, session_factory=db.session, provider_for=original.__getitem__)
    assert len(read_manifest(snapshot)) == 3
    assert db.updates == 0
    db.rows = rows[:2]  # Snapshot can contain uploads made after the database dump.
    restored = {"local": LocalStorageProvider(tmp_path / "fresh-local"), "minio": FakeProvider()}
    await import_snapshot(snapshot, session_factory=db.session, provider_for=restored.__getitem__)
    assert not await restored["local"].exists(rows[0].storage_key)
    assert not restored["minio"].objects
    await import_snapshot(snapshot, apply=True, session_factory=db.session, provider_for=restored.__getitem__)
    assert await restored["local"].load(rows[0].storage_key) == b"hello storage"
    assert restored["minio"].objects == {rows[1].storage_key: b"hello storage"}
    await import_snapshot(snapshot, apply=True, session_factory=db.session, provider_for=restored.__getitem__)
    assert len(restored["minio"].commits) == 1
    assert db.updates == 0


@pytest.mark.parametrize("failure", ["missing", "metadata", "corrupt", "destination"])
async def test_snapshot_import_preflight_refuses_all_writes(tmp_path, failure):
    rows = [row(), row("second/file.txt", backend="minio")]
    db = FakeDatabase(rows)
    providers = {"local": FakeProvider({rows[0].storage_key: b"hello storage"}),
                 "minio": FakeProvider({rows[1].storage_key: b"hello storage"})}
    await export_snapshot(tmp_path, session_factory=db.session, provider_for=providers.__getitem__)
    target = {"local": FakeProvider(), "minio": FakeProvider()}
    if failure == "missing":
        db.rows.append(row("uncovered"))
    elif failure == "metadata":
        rows[1].size_bytes += 1
    elif failure == "corrupt":
        (tmp_path / "minio" / rows[1].storage_key).write_bytes(b"corrupt")
    else:
        target["minio"].objects[rows[1].storage_key] = b"do not overwrite"
    with pytest.raises(ValueError):
        await import_snapshot(tmp_path, apply=True, session_factory=db.session, provider_for=target.__getitem__)
    assert not target["local"].commits and not target["minio"].commits


async def test_failed_export_has_no_complete_manifest_and_resumes(tmp_path):
    item = row()
    db = FakeDatabase([item])
    source = FakeProvider({item.storage_key: b"bad"})
    with pytest.raises(ValueError):
        await export_snapshot(tmp_path, session_factory=db.session, provider_for=lambda _: source)
    assert not (tmp_path / "manifest.json").exists()
    source.objects[item.storage_key] = b"hello storage"
    await export_snapshot(tmp_path, session_factory=db.session, provider_for=lambda _: source)
    assert len(read_manifest(tmp_path)) == 1
    with pytest.raises(ValueError, match="already complete"):
        await export_snapshot(tmp_path, session_factory=db.session)


@pytest.mark.parametrize("key", ["../outside", "/etc/passwd", "a/../../outside", "a//file", "a/./file", "a\\file", ".", ""])
def test_manifest_rejects_unsafe_paths(tmp_path, key):
    record = FileRecord.from_row(row())
    entry = {"id": record.id, "backend": "local", "key": key, "sha256": record.sha256,
             "size": record.size, "source": "stored_files"}
    (tmp_path / "manifest.json").write_text(json.dumps({"version": 1, "files": [entry]}))
    with pytest.raises(ValueError):
        read_manifest(tmp_path)


async def test_manifest_rejects_symlink_sources(tmp_path):
    item = row()
    db = FakeDatabase([item])
    await export_snapshot(tmp_path / "snapshot", session_factory=db.session,
                          provider_for=lambda _: FakeProvider({item.storage_key: b"hello storage"}))
    path = tmp_path / "snapshot" / "local" / item.storage_key
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        read_manifest(tmp_path / "snapshot")


async def test_zero_byte_stream_copy():
    item = row(data=b"")
    target = FakeProvider()
    await copy_verified(FakeProvider({item.storage_key: b""}), target, FileRecord.from_row(item), apply=True)
    assert target.objects == {item.storage_key: b""}


async def test_materials_are_migrated_and_snapshotted_with_derived_checksum(tmp_path):
    file = row()
    material = row("admin-materials/project/original.txt", source="project_materials")
    material.id = file.id  # IDs from different tables must remain independent.
    del material.sha256
    db = FakeDatabase([file, material])
    source = FakeProvider({file.storage_key: b"hello storage", material.storage_key: b"hello storage"})
    target = FakeProvider()
    providers = {"local": source, "minio": target}
    assert await migrate("local", "minio", apply=True, session_factory=db.session,
                         provider_for=providers.__getitem__) == 0
    assert file.storage_backend == material.storage_backend == "minio"
    assert target.objects == source.objects
    await export_snapshot(tmp_path, session_factory=db.session, provider_for=providers.__getitem__)
    manifest = await verify_snapshot(tmp_path)
    assert set(manifest) == {("stored_files", str(file.id)), ("project_materials", str(material.id))}
    assert manifest[("project_materials", str(material.id))].sha256 == hashlib.sha256(b"hello storage").hexdigest()
    restored = FakeProvider()
    await import_snapshot(tmp_path, apply=True, session_factory=db.session, provider_for=lambda _: restored)
    assert restored.objects == source.objects
    assert not hasattr(material, "sha256")


@pytest.mark.parametrize("corrupt", [False, True])
async def test_snapshot_payload_preflight_without_database(tmp_path, corrupt):
    item = row()
    await export_snapshot(tmp_path, session_factory=FakeDatabase([item]).session,
                          provider_for=lambda _: FakeProvider({item.storage_key: b"hello storage"}))
    payload = tmp_path / "local" / item.storage_key
    if corrupt:
        payload.write_bytes(b"broken")
    else:
        payload.unlink()
    with pytest.raises(ValueError if corrupt else FileNotFoundError):
        await verify_snapshot(tmp_path)
