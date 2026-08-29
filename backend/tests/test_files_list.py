"""文件列表与版本历史接口测试。

- GET /files：只返回当前版本（被取代的旧版本不出现），项目内全员可见；
- GET /files/{id}/versions：同名文档全部版本，新→旧；
- 跨项目 404。
"""

import uuid
from pathlib import Path

import httpx
import pytest

from app.domains.project.models import Project
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from tests.conftest import add_member, auth_headers

ALICE_PW = "Alice123!"
BOB_PW = "Bob12345!"


@pytest.fixture
def storage(tmp_path: Path):
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


async def _upload(
    client: httpx.AsyncClient, headers: dict[str, str], filename: str, content: str
) -> dict:
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": (filename, content.encode(), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_list_files_only_current_versions(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    await _upload(client, headers, "guide.md", "v1")
    v2 = await _upload(client, headers, "guide.md", "v2")
    other = await _upload(client, headers, "notes.md", "notes")

    resp = await client.get("/api/v1/files", headers=headers)

    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert ids == {v2["id"], other["id"]}  # 旧版本不出现


async def test_list_file_versions_newest_first(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    v1 = await _upload(client, headers, "guide.md", "v1")
    await _upload(client, headers, "guide.md", "v2")
    v3 = await _upload(client, headers, "guide.md", "v3")

    resp = await client.get(f"/api/v1/files/{v1['id']}/versions", headers=headers)

    assert resp.status_code == 200, resp.text
    versions = [row["version"] for row in resp.json()]
    assert versions == [3, 2, 1]
    assert resp.json()[0]["id"] == v3["id"]


async def test_versions_cross_project_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project, storage
) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    headers_a = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    uploaded = await _upload(client, headers_a, "guide.md", "v1")

    _, bob = await add_member(project_b, "bob", BOB_PW)
    headers_b = await auth_headers(client, "bob", BOB_PW, project_id=str(project_b.id))
    resp = await client.get(f"/api/v1/files/{uploaded['id']}/versions", headers=headers_b)
    assert resp.status_code == 404

    # B 项目成员也看不到 A 项目的文件列表
    list_resp = await client.get("/api/v1/files", headers=headers_b)
    assert list_resp.status_code == 200
    assert list_resp.json() == []
