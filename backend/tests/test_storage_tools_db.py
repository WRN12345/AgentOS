"""针对两种持久化文件记录模型验证运维 SQL。"""

import hashlib
import uuid

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError

from app.domains.files.models import StoredFile
from app.domains.requirements.models import Material
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.scripts.migrate_file_storage import migrate
from app.scripts.storage_snapshot import export_snapshot, import_snapshot, verify_snapshot


async def test_mixed_file_records_migrate_snapshot_restore(project, leader, admin_user, tmp_path):
    content = b"file and material originals"
    file_id = uuid.uuid4()
    keys = [f"projects/{project.id}/files/{file_id}", f"admin-materials/{project.id}/{file_id}"]
    local = LocalStorageProvider(tmp_path / "original")
    destination = LocalStorageProvider(tmp_path / "destination")
    destination.backend_name = "minio"
    providers = {"local": local, "minio": destination}
    for key in keys:
        await local.save(key, content)
    async with async_session_factory() as session:
        session.add_all([
            StoredFile(id=file_id, project_id=project.id, uploaded_by=leader.id,
                       original_filename="file.txt", storage_key=keys[0], storage_backend="local",
                       size_bytes=len(content), mime_type="text/plain", sha256=hashlib.sha256(content).hexdigest()),
            Material(id=file_id, project_id=project.id, uploaded_by=admin_user.id,
                     original_filename="material.txt", storage_key=keys[1], storage_backend="local",
                     size_bytes=len(content), chunks=[{"id": str(uuid.uuid4()), "text": "extracted content"}]),
        ])
        await session.commit()

    assert await migrate("local", "minio", provider_for=providers.__getitem__) == 0
    assert not await destination.exists(keys[0])
    assert await migrate("local", "minio", apply=True, provider_for=providers.__getitem__) == 0
    async with async_session_factory() as session:
        assert (await session.get(StoredFile, file_id)).storage_backend == "minio"
        assert (await session.get(Material, file_id)).storage_backend == "minio"
    for key in keys:
        assert await local.load(key) == await destination.load(key) == content

    snapshot = tmp_path / "snapshot"
    await export_snapshot(snapshot, provider_for=providers.__getitem__)
    assert len(await verify_snapshot(snapshot)) == 2
    restored = LocalStorageProvider(tmp_path / "restored")
    await import_snapshot(snapshot, apply=True, provider_for=lambda _: restored)
    for key in keys:
        assert await restored.load(key) == content
    # 存储迁移仅更改存储后端，原始证据保持不可变。
    for statement in (
        update(Material).where(Material.id == file_id).values(storage_backend="local", original_filename="changed.txt"),
        update(Material).where(Material.id == file_id).values(storage_backend="unsupported"),
        delete(Material).where(Material.id == file_id),
    ):
        async with async_session_factory() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(statement)
            await session.rollback()
