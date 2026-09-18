"""Verified, resumable storage migration. Pause application writes while running."""

import argparse
import asyncio
import hashlib
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from sqlalchemy import select, text, update
from sqlalchemy.orm import defer

from app.domains.files.models import StoredFile
from app.domains.requirements.models import Material
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.provider import StorageProvider, storage_for

BACKENDS = ("local", "minio")
FILE_MODELS = {"stored_files": StoredFile, "project_materials": Material}
# Shared by migration and snapshot commands, including migrations in reverse.
STORAGE_OPERATION_LOCK = 724190382610


@dataclass(frozen=True)
class FileRecord:
    id: str
    backend: str
    key: str
    sha256: str | None
    size: int
    source: str = "stored_files"

    @classmethod
    def from_row(cls, row, source="stored_files"):
        return cls(str(row.id), row.storage_backend, row.storage_key,
                   row.sha256 if source == "stored_files" else None, row.size_bytes, source)


@asynccontextmanager
async def operation_lock(session_factory):
    # A separate transaction holds the lock while per-file transactions commit.
    async with session_factory() as session:
        async with session.begin():
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": STORAGE_OPERATION_LOCK},
            )
            if not acquired:
                raise RuntimeError("Another storage migration/snapshot operation is running")
            yield


async def verify(provider: StorageProvider, key: str, record: FileRecord) -> FileRecord:
    digest = hashlib.sha256()
    size = 0
    async for chunk in provider.iter_chunks(key):
        digest.update(chunk)
        size += len(chunk)
    sha256 = digest.hexdigest()
    if size != record.size or (record.sha256 is not None and sha256 != record.sha256):
        raise ValueError(f"Integrity mismatch for {record.id} at {key}")
    return replace(record, sha256=sha256)


async def copy_verified(source, target, record, *, source_key=None, target_key=None, apply=False):
    """Never delete published objects, even after an uncertain commit outcome."""
    source_key = record.key if source_key is None else source_key
    target_key = record.key if target_key is None else target_key
    if await target.exists(target_key):
        record = await verify(source, source_key, record)
        await verify(target, target_key, record)
        return record
    if not apply:
        return await verify(source, source_key, record)

    staged = await target.stage()
    try:
        digest = hashlib.sha256()
        size = 0
        async for chunk in source.iter_chunks(source_key):
            digest.update(chunk)
            size += len(chunk)
            await staged.write(chunk)
        sha256 = digest.hexdigest()
        if size != record.size or (record.sha256 is not None and sha256 != record.sha256):
            raise ValueError(f"Source integrity mismatch for {record.id}")
        record = replace(record, sha256=sha256)
        # Cooperating tools are serialized. Application writes must be paused;
        # the provider interface itself has no conditional-create primitive.
        if await target.exists(target_key):
            await verify(target, target_key, record)
        else:
            await target.commit(staged, target_key)
        await verify(target, target_key, record)
        return record
    finally:
        # Only temporary data is discarded, never the destination key.
        await target.discard(staged)


async def migrate(from_backend, to_backend, *, apply=False,
                  session_factory=async_session_factory, provider_for=storage_for) -> int:
    if from_backend not in BACKENDS or to_backend not in BACKENDS or from_backend == to_backend:
        raise ValueError("Choose two different supported backends")
    failures = 0
    async with operation_lock(session_factory):
        async with session_factory() as session:
            ids = []
            for source_name, model in FILE_MODELS.items():
                ids.extend((source_name, file_id) for file_id in (await session.scalars(
                    select(model.id).where(model.storage_backend == from_backend)
                    .order_by(model.id)
                )).all())
        print(f"{'Migrate' if apply else 'Dry run'}: {len(ids)} records, including all versions")
        source, target = provider_for(from_backend), provider_for(to_backend)
        for source_name, file_id in ids:
            model = FILE_MODELS[source_name]
            try:
                async with session_factory() as session:
                    async with session.begin():
                        query = select(model).where(model.id == file_id).with_for_update()
                        if model is Material:
                            query = query.options(defer(Material.chunks))
                        row = await session.scalar(query)
                        if row is None or row.storage_backend != from_backend:
                            raise RuntimeError(f"Record changed during migration: {file_id}")
                        await copy_verified(source, target, FileRecord.from_row(row, source_name), apply=apply)
                        if apply:
                            # Core UPDATE avoids loading unrelated ORM models merely
                            # to resolve their foreign keys during an ORM flush.
                            await session.execute(
                                update(model.__table__).where(model.id == file_id)
                                .values(storage_backend=to_backend)
                            )
                print(f"Verified {source_name}/{file_id}")
            except Exception as exc:
                failures += 1
                print(f"FAILED {source_name}/{file_id}: {type(exc).__name__}", file=sys.stderr)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=(
        "Copy all file versions, verify SHA-256/size, then change backend. Sources are retained. "
        "Pause application writes and other storage operations. Dry run unless --yes."
    ))
    parser.add_argument("--from-backend", required=True, choices=BACKENDS)
    parser.add_argument("--to-backend", required=True, choices=BACKENDS)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    try:
        return int(bool(asyncio.run(migrate(args.from_backend, args.to_backend, apply=args.yes))))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
