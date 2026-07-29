"""T6.1 新增测试（test_unit_* / test_audit_coverage）共用辅助。

只新增、不复用既有测试文件的私有 helper，避免与既有文件耦合；
conftest.py 提供的 fixtures（client/project/add_member/auth_headers）直接继续使用。
"""

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
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


@pytest.fixture
def storage(tmp_path: Path):
    """存储 Provider 注入为指向临时目录的 LocalStorageProvider（与 test_files_api 同方式）。"""
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


async def make_ctx(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """标准场景：leader（负责人）+ alice/bob/carol/dave 四名普通成员及各自请求头。"""
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
        "leader_headers": await auth_headers(client, "leader", LEADER_PW),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW),
        "bob_headers": await auth_headers(client, "bob", BOB_PW),
        "carol_headers": await auth_headers(client, "carol", CAROL_PW),
        "dave_headers": await auth_headers(client, "dave", DAVE_PW),
    }


async def create_work_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: uuid.UUID,
    *,
    due_at: str | None = "2026-08-01T00:00:00Z",
    title: str = "RAG 工作项",
) -> dict:
    """负责人创建工作项（DRAFT，version=1）。"""
    payload: dict[str, object] = {
        "title": title,
        "description": "实现 RAG",
        "acceptance_criteria": "评测集通过",
        "priority": "high",
        "assignee_id": str(assignee_id),
    }
    if due_at is not None:
        payload["due_at"] = due_at
    resp = await client.post("/api/v1/work-items", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def command_work_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    command: str,
    version: int,
) -> httpx.Response:
    """执行工作项状态命令（publish/start/block/unblock/submit/cancel）。"""
    return await client.post(
        f"/api/v1/work-items/{item_id}/{command}", json={"version": version}, headers=headers
    )


async def publish_work_item(
    client: httpx.AsyncClient, headers: dict[str, str], item: dict
) -> dict:
    """发布工作项：DRAFT(v1) → READY(v2)。"""
    resp = await command_work_item(client, headers, item["id"], "publish", 1)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def get_work_item(client: httpx.AsyncClient, headers: dict[str, str], item_id: str) -> dict:
    resp = await client.get(f"/api/v1/work-items/{item_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def create_collaboration(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    assignee_id: uuid.UUID,
    *,
    due_at: str | None = "2026-07-30T00:00:00Z",
    title: str = "标注训练样本",
) -> dict:
    """主执行人发起协作请求（REQUESTED，version=1）。"""
    payload: dict[str, object] = {
        "assignee_id": str(assignee_id),
        "title": title,
        "goal": "完成 100 条样本标注",
    }
    if due_at is not None:
        payload["due_at"] = due_at
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests", json=payload, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def command_collaboration(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    collab_id: str,
    command: str,
    version: int,
    **extra: object,
) -> httpx.Response:
    """执行协作请求状态命令（accept/decline/start/submit/request-revision/complete/cancel）。"""
    return await client.post(
        f"/api/v1/collaboration-requests/{collab_id}/{command}",
        json={"version": version, **extra},
        headers=headers,
    )


async def create_transfer(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    to_member_id: uuid.UUID,
) -> httpx.Response:
    """发起转派申请。"""
    return await client.post(
        f"/api/v1/work-items/{item_id}/transfer-requests",
        json={
            "to_member_id": str(to_member_id),
            "reason": "超出我的能力范围",
            "impact_note": "DDL 不变，进行中的协作请求由新负责人接管",
        },
        headers=headers,
    )


async def create_deadline_change(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    target_type: str,
    target_id: str,
    new_due_at: str,
) -> httpx.Response:
    """发起 DDL 变更申请。"""
    return await client.post(
        f"/api/v1/work-items/{item_id}/deadline-change-requests",
        json={
            "target_type": target_type,
            "target_id": target_id,
            "new_due_at": new_due_at,
            "reason": "依赖方延期，需要顺延",
        },
        headers=headers,
    )


async def create_deliverable(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    **extra: object,
) -> dict:
    """主执行人提交交付物（新版本），默认 text 类型，可用 extra 覆盖为 git_link/file。"""
    payload = {"type": "text", "content": "交付说明", **extra}
    # file 类型不允许携带 content（git_link 的 content 是链接，text 的是正文）
    if payload["type"] == "file" and "content" not in extra:
        payload.pop("content")
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables", json=payload, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def upload_file(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = b"RAG evaluation report\n",
    filename: str = "report.txt",
    mime: str = "text/plain",
    work_item_id: str | None = None,
) -> httpx.Response:
    """上传文件（可选关联工作项）。"""
    data = {"work_item_id": str(work_item_id)} if work_item_id else {}
    return await client.post(
        "/api/v1/files", headers=headers, files={"file": (filename, content, mime)}, data=data
    )


async def audit_events_for(target_id: str) -> list[AuditEvent]:
    """按创建时间正序取某目标对象的全部审计事件。"""
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.target_id == uuid.UUID(target_id))
                    .order_by(AuditEvent.created_at)
                )
            )
            .scalars()
            .all()
        )


async def audit_events_by_action(action: str) -> list[AuditEvent]:
    """按动作名取全部审计事件。"""
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == action)
                )
            )
            .scalars()
            .all()
        )
