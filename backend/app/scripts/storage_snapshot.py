"""Portable verified object snapshots; database dumps are handled separately."""

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.domains.requirements.models import Material
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import storage_for
from app.scripts.migrate_file_storage import (
    BACKENDS, FILE_MODELS, FileRecord, copy_verified, operation_lock, verify,
)


def safe_path(root: Path, key: str) -> Path:
    """Accept only canonical relative keys and reject symlinks within snapshots."""
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
        raise ValueError("Invalid snapshot key")
    parts = PurePosixPath(key).parts
    if key.startswith("/") or any(p in (".", "..") for p in parts) or "/".join(parts) != key:
        raise ValueError(f"Unsafe snapshot key: {key!r}")
    path = root
    if path.is_symlink():
        raise ValueError("Snapshot root cannot be a symlink")
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Snapshot symlink refused: {path}")
    return path


def validate_record(record: FileRecord, *, require_hash: bool = True) -> None:
    if not isinstance(record.source, str) or record.source not in FILE_MODELS:
        raise ValueError("Unsupported record source")
    if record.backend not in BACKENDS:
        raise ValueError(f"Unsupported backend: {record.backend}")
    if not isinstance(record.id, str) or str(uuid.UUID(record.id)) != record.id:
        raise ValueError("Invalid record ID")
    if record.sha256 is None and not require_hash:
        pass
    elif not isinstance(record.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", record.sha256):
        raise ValueError("Invalid SHA-256")
    if type(record.size) is not int or record.size < 0:
        raise ValueError("Invalid size")


def read_manifest(root: Path) -> dict[tuple[str, str], FileRecord]:
    with safe_path(root, "manifest.json").open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Invalid or incomplete snapshot manifest")
    records = {}
    keys = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "backend", "key", "sha256", "size", "source"}:
            raise ValueError("Invalid manifest record")
        record = FileRecord(**entry)
        validate_record(record)
        safe_path(root, record.key)
        safe_path(root, f"{record.backend}/{record.key}")
        identity = (record.source, record.id)
        if identity in records or (record.backend, record.key) in keys:
            raise ValueError("Duplicate manifest record or object key")
        records[identity] = record
        keys.add((record.backend, record.key))
    return records


async def database_records(session_factory):
    async with session_factory() as session:
        records = []
        for source, model in FILE_MODELS.items():
            query = select(model).order_by(model.id)
            if model is Material:
                query = query.options(defer(Material.chunks))
            rows = (await session.scalars(query)).all()
            records.extend(FileRecord.from_row(row, source) for row in rows)
        return records


async def verify_snapshot(root: Path) -> dict[tuple[str, str], FileRecord]:
    """Validate all archived payloads without accessing or changing a database."""
    records = read_manifest(root)
    source = LocalStorageProvider(root)
    for record in records.values():
        key = f"{record.backend}/{record.key}"
        safe_path(root, key)
        await verify(source, key, record)
    return records


async def export_snapshot(output, *, session_factory=async_session_factory, provider_for=storage_for):
    root = Path(output).absolute()
    manifest_path = safe_path(root, "manifest.json")
    if manifest_path.exists():
        raise ValueError("Snapshot already complete; use a new output directory")
    root.mkdir(parents=True, exist_ok=True)
    safe_path(root, ".tmp")
    async with operation_lock(session_factory):
        records = await database_records(session_factory)
        target = LocalStorageProvider(root)
        verified = []
        for record in records:
            validate_record(record, require_hash=False)
            safe_path(root, record.key)
            key = f"{record.backend}/{record.key}"
            safe_path(root, key)
            verified.append(await copy_verified(provider_for(record.backend), target, record,
                                                target_key=key, apply=True))
        # The manifest is the complete marker. A failed export has no manifest.
        temporary = safe_path(root, f".manifest-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump({"version": 1, "files": [asdict(record) for record in verified]}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Exported and verified {len(records)} records to {root}")


async def import_snapshot(input_path, *, apply=False, session_factory=async_session_factory,
                          provider_for=storage_for):
    root = Path(input_path).absolute()
    manifest = read_manifest(root)
    async with operation_lock(session_factory):
        records = await database_records(session_factory)
        selected = []
        for record in records:
            expected = manifest.get((record.source, record.id))
            if expected is None:
                raise ValueError(f"Snapshot does not match restored database record {record.id}")
            # Materials predate persisted checksums; the snapshot carries their source hash.
            if record.sha256 is None:
                record = replace(record, sha256=expected.sha256)
            if expected != record:
                raise ValueError(f"Snapshot does not match restored database record {record.id}")
            selected.append(record)
        source = LocalStorageProvider(root)
        # Preflight all selected content and destinations before writing anything.
        # Extra snapshot records are allowed: uploads may follow the DB dump.
        for record in selected:
            key = f"{record.backend}/{record.key}"
            safe_path(root, key)
            await copy_verified(source, provider_for(record.backend), record, source_key=key)
        if apply:
            for record in selected:
                key = f"{record.backend}/{record.key}"
                safe_path(root, key)
                await copy_verified(source, provider_for(record.backend), record,
                                    source_key=key, apply=True)
    print(f"{'Restored' if apply else 'Dry run: verified'} {len(records)} records; database unchanged")


def main() -> int:
    parser = argparse.ArgumentParser(description=(
        "Export/import all file versions from both backends with SHA-256/size verification. "
        "Pause writes and migrations during backup/restore. Import uses DATABASE_URL to "
        "validate the restored DB and the configured MinIO bucket; no DB changes."
    ))
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", required=True)
    restore = commands.add_parser("import")
    restore.add_argument("--input", required=True)
    restore.add_argument("--yes", action="store_true", help="Write objects; default is verification only")
    for command in (export, restore):
        command.add_argument("--local-root", type=Path, help="Override local storage root (e.g. an isolated restore target)")
    args = parser.parse_args()

    def provider_for(backend):
        if backend == "local" and args.local_root is not None:
            return LocalStorageProvider(args.local_root)
        return storage_for(backend)

    try:
        if args.command == "export":
            asyncio.run(export_snapshot(args.output, provider_for=provider_for))
        else:
            asyncio.run(import_snapshot(args.input, apply=args.yes, provider_for=provider_for))
        return 0
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
