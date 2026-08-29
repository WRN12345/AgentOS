"""知识文档版本化测试。

- 同项目同名上传 = 新版本：v 递增，旧版本 superseded_by 指向新版本；
- 不同名/不同项目互不影响，各自从 v1 开始；
- 上传响应与审计事件携带版本信息；
- .docx 扩展名与 MIME 位于允许列表。
"""

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.files.models import StoredFile
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from tests.conftest import add_member, auth_headers

ALICE_PW = "Alice123!"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def storage(tmp_path: Path):
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = b"content",
    filename: str = "guide.md",
    mime: str = "text/markdown",
) -> httpx.Response:
    return await client.post(
        "/api/v1/files", headers=headers, files={"file": (filename, content, mime)}
    )


async def test_same_name_upload_creates_new_version(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    resp1 = await _upload(client, headers, content=b"v1 content")
    assert resp1.status_code == 201, resp1.text
    resp2 = await _upload(client, headers, content=b"v2 content")
    assert resp2.status_code == 201, resp2.text

    v1, v2 = resp1.json(), resp2.json()
    assert v1["version"] == 1 and v1["superseded_by"] is None  # 响应是上传时刻快照
    assert v2["version"] == 2

    async with async_session_factory() as session:
        old = await session.get(StoredFile, uuid.UUID(v1["id"]))
        new = await session.get(StoredFile, uuid.UUID(v2["id"]))
        assert old is not None and new is not None
        assert old.superseded_by == new.id  # 旧版本已标记被取代（保留不删除）
        assert new.superseded_by is None
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "file.uploaded",
                    AuditEvent.target_id == new.id,
                )
            )
        ).scalar_one()
    assert event.after["version"] == 2
    assert event.after["supersedes"] == v1["id"]


async def test_third_version_chains_correctly(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    ids = [
        (await _upload(client, headers, content=f"v{i}".encode())).json()["id"]
        for i in range(1, 4)
    ]

    async with async_session_factory() as session:
        rows = [
            await session.get(StoredFile, uuid.UUID(file_id)) for file_id in ids
        ]
    assert [r.version for r in rows if r is not None] == [1, 2, 3]
    assert rows[0].superseded_by == rows[1].id  # type: ignore[union-attr]
    assert rows[1].superseded_by == rows[2].id  # type: ignore[union-attr]
    assert rows[2].superseded_by is None  # type: ignore[union-attr]


async def test_different_names_independent_versions(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    resp1 = await _upload(client, headers, filename="a.md")
    resp2 = await _upload(client, headers, filename="b.md")

    assert resp1.json()["version"] == 1
    assert resp2.json()["version"] == 1


async def test_docx_in_upload_whitelist(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    resp = await _upload(
        client, headers, filename="spec.docx", mime=DOCX_MIME, content=b"fake docx"
    )

    assert resp.status_code == 201, resp.text
    assert ".docx" in settings.allowed_upload_extensions
    assert DOCX_MIME in settings.allowed_upload_mime_types


def test_no_file_delete_endpoint_anywhere() -> None:
    """所有路由（含 admin）都不提供文件删除端点。"""
    delete_routes = [
        route.path
        for route in app.routes
        if hasattr(route, "methods") and "DELETE" in route.methods  # type: ignore[attr-defined]
    ]
    assert not any("files" in path for path in delete_routes), delete_routes
