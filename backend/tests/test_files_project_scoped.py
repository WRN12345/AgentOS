"""存储文件的项目隔离测试。

覆盖行为：
- 落库归属：当前项目上下文上传的文件 project_id = 当前项目（经 actor 派生填充）
- 下载越权：A 上下文访问 B 项目文件 → 404（不是 403，不暴露存在性）
- 跨实体引用：A 上下文上传关联 B 项目工作项 → 404
- 项目互不泄漏：A/B 各自上传的文件互不可见，响应均带正确 project_id
"""

import httpx
import pytest
from pathlib import Path

from sqlalchemy import select

from app.domains.files.models import StoredFile
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"

CONTENT = b"project-scoped file content\n"


@pytest.fixture
def storage(tmp_path: Path):
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


async def _setup_project(
    client: httpx.AsyncClient, project: Project, *, tag: str
) -> dict[str, object]:
    """在给定项目内准备 leader + alice（tag 前缀区分项目）。"""
    _, leader = await add_member(
        project, f"{tag}_leader", LEADER_PW, role="leader", display_name="负责人"
    )
    _, alice = await add_member(project, f"{tag}_alice", ALICE_PW, display_name="爱丽丝")
    return {
        "leader": leader,
        "alice": alice,
        "leader_headers": await auth_headers(
            client, f"{tag}_leader", LEADER_PW, project_id=str(project.id)
        ),
        "alice_headers": await auth_headers(
            client, f"{tag}_alice", ALICE_PW, project_id=str(project.id)
        ),
    }


async def _create_item(
    client: httpx.AsyncClient, headers: dict[str, str], assignee_id: str
) -> str:
    resp = await client.post(
        "/api/v1/work-items",
        json={
            "title": "RAG 工作项",
            "description": "实现 RAG",
            "priority": "high",
            "assignee_id": assignee_id,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = CONTENT,
    filename: str = "report.txt",
    mime: str = "text/plain",
    work_item_id: str | None = None,
) -> httpx.Response:
    data = {"work_item_id": str(work_item_id)} if work_item_id else {}
    return await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": (filename, content, mime)},
        data=data,
    )


async def test_upload_row_project_id_from_actor(
    client: httpx.AsyncClient,
    project_a: Project,
    storage: LocalStorageProvider,
) -> None:
    """A 上下文上传：落库 project_id = A，响应带正确 project_id。"""
    ctx = await _setup_project(client, project_a, tag="a")
    resp = await _upload(client, ctx["alice_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_id"] == str(project_a.id)

    async with async_session_factory() as session:
        row = (await session.execute(select(StoredFile))).scalar_one()
    assert row.project_id == project_a.id


async def test_download_cross_project_404(
    client: httpx.AsyncClient,
    project_a: Project,
    project_b: Project,
    storage: LocalStorageProvider,
) -> None:
    """A 项目上传的文件，B 项目成员（B 上下文）下载 → 404，不暴露存在性。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    uploaded = await _upload(client, ctx_a["alice_headers"])  # type: ignore[arg-type]
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]

    resp = await client.get(
        f"/api/v1/files/{file_id}/download",
        headers=ctx_b["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_upload_cross_project_work_item_404(
    client: httpx.AsyncClient,
    project_a: Project,
    project_b: Project,
    storage: LocalStorageProvider,
) -> None:
    """B 上下文上传关联 A 项目工作项 → 404（跨实体引用同项目校验）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_id = await _create_item(client, ctx_a["leader_headers"], str(ctx_a["alice"].id))  # type: ignore[arg-type, attr-defined]

    resp = await _upload(client, ctx_b["alice_headers"], work_item_id=item_id)  # type: ignore[arg-type]
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_projects_files_mutually_invisible(
    client: httpx.AsyncClient,
    project_a: Project,
    project_b: Project,
    storage: LocalStorageProvider,
) -> None:
    """A/B 各自上传的文件互不可见：双方仅能下载本项目文件，行归属各自项目。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    up_a = await _upload(client, ctx_a["alice_headers"])  # type: ignore[arg-type]
    up_b = await _upload(client, ctx_b["alice_headers"])  # type: ignore[arg-type]
    assert up_a.status_code == 201
    assert up_b.status_code == 201
    file_a, file_b = up_a.json()["id"], up_b.json()["id"]
    assert up_a.json()["project_id"] == str(project_a.id)
    assert up_b.json()["project_id"] == str(project_b.id)

    resp = await client.get(
        f"/api/v1/files/{file_b}/download", headers=ctx_a["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    resp = await client.get(
        f"/api/v1/files/{file_a}/download", headers=ctx_a["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200

    resp = await client.get(
        f"/api/v1/files/{file_a}/download", headers=ctx_b["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    resp = await client.get(
        f"/api/v1/files/{file_b}/download", headers=ctx_b["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200

    async with async_session_factory() as session:
        rows = (await session.execute(select(StoredFile))).scalars().all()
    assert {row.project_id for row in rows} == {project_a.id, project_b.id}
