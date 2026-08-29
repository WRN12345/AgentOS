"""文件上传和下载 API 集成测试。

Provider 通过 app.dependency_overrides 注入指向临时目录的 LocalStorageProvider，
用于验证业务层只经 StorageProvider 接口与存储交互。
"""

import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.collaboration.models import CollaborationRequest
from app.domains.files.models import StoredFile
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"
CAROL_PW = "Carol123!"
DAVE_PW = "Dave123!"

CONTENT = b"RAG evaluation report\nline2\n"


@pytest.fixture
def storage(tmp_path: Path):
    """将存储 Provider 注入指向临时目录的 LocalStorageProvider。"""
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


def _files_on_disk(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """leader + alice（主执行人）+ bob（协作者）+ carol（协作请求方）+ dave（无关成员）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    _, carol = await add_member(project, "carol", CAROL_PW, display_name="卡罗尔")
    _, dave = await add_member(project, "dave", DAVE_PW, display_name="戴夫")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "dave": dave,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
        "carol_headers": await auth_headers(client, "carol", CAROL_PW, project_id=str(project.id)),
        "dave_headers": await auth_headers(client, "dave", DAVE_PW, project_id=str(project.id)),
    }


async def _create_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: str,
    collaborator_ids: list[str] | None = None,
) -> str:
    resp = await client.post(
        "/api/v1/work-items",
        json={
            "title": "RAG 工作项",
            "description": "实现 RAG",
            "priority": "high",
            "assignee_id": assignee_id,
            "collaborator_ids": collaborator_ids or [],
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


async def test_upload_success_persists_row_and_matching_hash(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """合法文件：stored_files 落库，磁盘文件 SHA-256 与库中一致，写审计事件。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[attr-defined]

    resp = await _upload(client, ctx["alice_headers"], work_item_id=item_id)  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text
    body = resp.json()
    expected_sha = hashlib.sha256(CONTENT).hexdigest()
    assert body["sha256"] == expected_sha
    assert body["size_bytes"] == len(CONTENT)
    assert body["mime_type"] == "text/plain"
    assert body["original_filename"] == "report.txt"
    assert body["storage_backend"] == "local"
    assert body["work_item_id"] == item_id
    assert "storage_key" not in body  # 存储内部键不属于公开 API。

    async with async_session_factory() as session:
        row = (await session.execute(select(StoredFile))).scalar_one()
        assert row.sha256 == expected_sha
        assert row.uploaded_by == alice.id  # type: ignore[attr-defined]
        assert str(row.work_item_id) == item_id
        assert not row.storage_key.startswith("/")  # 相对键避免把主机路径写入数据库。
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "file.uploaded")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_id == row.id
    assert events[0].actor_id == alice.user_id  # type: ignore[attr-defined]
    assert events[0].after["sha256"] == expected_sha

    on_disk = await storage.load(row.storage_key)
    assert on_disk == CONTENT
    assert hashlib.sha256(on_disk).hexdigest() == row.sha256


async def test_upload_requires_login(client: httpx.AsyncClient, project: Project, storage) -> None:
    resp = await client.post(
        "/api/v1/files", files={"file": ("a.txt", b"hi", "text/plain")}
    )
    assert resp.status_code == 401


async def test_upload_with_missing_work_item_404(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    ctx = await _setup(client, project)
    resp = await _upload(
        client,
        ctx["alice_headers"],  # type: ignore[arg-type]
        work_item_id=str(uuid.uuid4()),
    )
    assert resp.status_code == 404
    assert _files_on_disk(storage._root) == []


async def test_upload_oversize_rejected_no_residue(
    client: httpx.AsyncClient,
    project: Project,
    storage: LocalStorageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过大小上限 → 413，无库存记录、磁盘（含暂存区）无残留。"""
    monkeypatch.setattr(settings, "upload_max_bytes", 10)
    ctx = await _setup(client, project)
    resp = await _upload(client, ctx["alice_headers"], content=b"x" * 100)  # type: ignore[arg-type]
    assert resp.status_code == 413
    assert resp.json()["code"] == "FILE_TOO_LARGE"

    async with async_session_factory() as session:
        count = len((await session.execute(select(StoredFile))).scalars().all())
    assert count == 0
    assert _files_on_disk(storage._root) == []


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("evil.exe", "text/plain"),  # 扩展名不在白名单
        ("report.txt", "application/octet-stream"),  # MIME 不在白名单
    ],
)
async def test_upload_disallowed_type_rejected_no_residue(
    client: httpx.AsyncClient,
    project: Project,
    storage: LocalStorageProvider,
    filename: str,
    mime: str,
) -> None:
    """非法扩展名/非法 MIME → 415，磁盘无残留。"""
    ctx = await _setup(client, project)
    resp = await _upload(client, ctx["alice_headers"], filename=filename, mime=mime)  # type: ignore[arg-type]
    assert resp.status_code == 415
    assert resp.json()["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert _files_on_disk(storage._root) == []


async def test_upload_db_failure_compensates_disk_file(
    client: httpx.AsyncClient,
    project: Project,
    storage: LocalStorageProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库失败时补偿删除已落盘文件，且不保留库存记录。"""
    import app.domains.files.service as files_service

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(files_service, "record_event", _boom)
    ctx = await _setup(client, project)
    # 应用未捕获异常经全局处理器返回 500 统一格式后仍会被 Starlette 重抛；
    # 用 raise_app_exceptions=False 的客户端验证响应侧行为
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        resp = await raw_client.post(
            "/api/v1/files",
            headers=ctx["alice_headers"],  # type: ignore[arg-type]
            files={"file": ("report.txt", CONTENT, "text/plain")},
        )
    assert resp.status_code == 500
    assert resp.json()["code"] == "INTERNAL_ERROR"

    async with async_session_factory() as session:
        count = len((await session.execute(select(StoredFile))).scalars().all())
    assert count == 0
    assert _files_on_disk(storage._root) == []


async def test_download_permission_matrix(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """仅工作项负责人、执行人、协作者和协作请求相关人可下载关联文件。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment]
    item_id = await _create_item(
        client, ctx["leader_headers"], str(alice.id), collaborator_ids=[str(bob.id)]  # type: ignore[arg-type]
    )
    # carol 与 alice 之间存在该工作项的协作请求（直接建库准备数据）
    async with async_session_factory() as session:
        session.add(
            CollaborationRequest(
                work_item_id=uuid.UUID(item_id),
                requester_id=alice.id,
                assignee_id=carol.id,
                title="整理销售资料",
                goal="按模板整理资料",
            )
        )
        await session.commit()

    uploaded = await _upload(client, ctx["alice_headers"], work_item_id=item_id)  # type: ignore[arg-type]
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]

    for key in ("leader_headers", "alice_headers", "bob_headers", "carol_headers"):
        resp = await client.get(f"/api/v1/files/{file_id}/download", headers=ctx[key])  # type: ignore[arg-type]
        assert resp.status_code == 200, f"{key} 应可下载: {resp.text}"
        assert resp.content == CONTENT
        assert resp.headers["content-type"].startswith("text/plain")
        assert "report.txt" in resp.headers["content-disposition"]

    denied = await client.get(
        f"/api/v1/files/{file_id}/download", headers=ctx["dave_headers"]  # type: ignore[arg-type]
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"


async def test_download_unlinked_knowledge_doc_all_members(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """项目内在职成员均可下载未关联工作项的知识库文档。"""
    ctx = await _setup(client, project)
    uploaded = await _upload(client, ctx["alice_headers"])  # type: ignore[arg-type]
    file_id = uploaded.json()["id"]

    for key in ("alice_headers", "leader_headers", "bob_headers", "dave_headers"):
        resp = await client.get(f"/api/v1/files/{file_id}/download", headers=ctx[key])  # type: ignore[arg-type]
        assert resp.status_code == 200, f"{key} 应可下载知识库文档: {resp.text}"
        assert resp.content == CONTENT


async def test_download_missing_or_cleaned_file_unified_error(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """记录不存在 / 物理文件已清理 → 统一错误格式 404。"""
    ctx = await _setup(client, project)
    missing = await client.get(
        f"/api/v1/files/{uuid.uuid4()}/download", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert missing.status_code == 404
    assert set(missing.json()) == {"code", "message", "request_id", "details"}

    uploaded = await _upload(client, ctx["alice_headers"])  # type: ignore[arg-type]
    file_id = uploaded.json()["id"]
    async with async_session_factory() as session:
        row = (await session.execute(select(StoredFile))).scalar_one()
    await storage.delete(row.storage_key)  # 模拟文件被清理

    cleaned = await client.get(
        f"/api/v1/files/{file_id}/download", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert cleaned.status_code == 404
    assert cleaned.json()["code"] == "NOT_FOUND"


async def test_guessing_storage_key_cannot_bypass_api(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """上传目录没有静态映射，猜测 storage_key 路径无法绕过 API。"""
    ctx = await _setup(client, project)
    uploaded = await _upload(client, ctx["alice_headers"])  # type: ignore[arg-type]
    assert uploaded.status_code == 201
    async with async_session_factory() as session:
        row = (await session.execute(select(StoredFile))).scalar_one()

    # 上传目录不经 API/静态服务暴露：所有直接路径猜测一律 404
    for path in (
        f"/uploads/{row.storage_key}",
        f"/files/{row.storage_key}",
        f"/api/v1/uploads/{row.storage_key}",
        "/api/v1/files/download",
    ):
        resp = await client.get(path, headers=ctx["dave_headers"])  # type: ignore[arg-type]
        assert resp.status_code == 404, path


async def test_download_writes_audit_event(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """下载动作在 audit_events 可查：操作者、目标文件、请求 ID。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    uploaded = await _upload(client, ctx["alice_headers"])  # type: ignore[arg-type]
    file_id = uploaded.json()["id"]

    resp = await client.get(
        f"/api/v1/files/{file_id}/download", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200

    async with async_session_factory() as session:
        leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "file.downloaded")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == leader.user_id
    assert str(event.target_id) == file_id
    assert event.target_type == "stored_file"
    assert event.request_id is not None
    assert event.after["original_filename"] == "report.txt"
