"""交付物版本化 API 集成测试（T4.4 验收，7.5、12.5、16、17.2 节）。

覆盖：三类交付物版本递增与历史可查、提交权限（仅主执行人）、submit 前置校验、
file 类型 sha256 追溯与归属校验、可见性（负责人/相关成员 vs 无关成员）、
协作回传引用交付物/文件。
"""

import hashlib
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

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
DAVE_PW = "Dave123!"

CONTENT = b"deliverable file content\n"


@pytest.fixture
def storage(tmp_path: Path):
    """存储 Provider 注入临时目录（与 test_files_api 同一模式）。"""
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """leader + alice（主执行人）+ bob（普通成员/协作者）+ dave（无关成员）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    _, dave = await add_member(project, "dave", DAVE_PW, display_name="戴夫")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "dave": dave,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW),
        "bob_headers": await auth_headers(client, "bob", BOB_PW),
        "dave_headers": await auth_headers(client, "dave", DAVE_PW),
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


async def _start_item(
    client: httpx.AsyncClient,
    ctx: dict[str, object],
    item_id: str,
) -> None:
    """publish + start，把 DRAFT 工作项推进到 IN_PROGRESS（version 到 3）。"""
    leader_headers = ctx["leader_headers"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]  # type: ignore[assignment]
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/publish", json={"version": 1}, headers=leader_headers
    )
    assert resp.status_code == 200, resp.text
    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/start", json={"version": 2}, headers=alice_headers
    )
    assert resp.status_code == 200, resp.text


async def _deliver(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    payload: dict[str, object],
) -> httpx.Response:
    return await client.post(
        f"/api/v1/work-items/{item_id}/deliverables", json=payload, headers=headers
    )


# ---------- 版本化与历史 ----------


async def test_three_submissions_create_versions_1_2_3_and_history(
    client: httpx.AsyncClient, project: Project
) -> None:
    """连续三次提交 → 版本 1/2/3，旧版本保留可查（7.5 节）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    for payload in (
        {"type": "text", "content": "第一版说明"},
        {"type": "git_link", "content": "https://git.example.com/repo/commit/abc"},
        {"type": "text", "content": "第三版说明"},
    ):
        resp = await _deliver(client, ctx["alice_headers"], item_id, payload)  # type: ignore[arg-type]
        assert resp.status_code == 201, resp.text

    history = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert history.status_code == 200
    versions = [d["version"] for d in history.json()]
    assert versions == [3, 2, 1]  # 倒序，全部保留

    # 每个历史版本内容均可查询
    first = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables/1", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert first.json()["content"] == "第一版说明"
    second = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables/2", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert second.json()["type"] == "git_link"
    assert second.json()["content"] == "https://git.example.com/repo/commit/abc"
    assert second.json()["submitted_by"]["id"] == str(alice.id)  # type: ignore[union-attr]


async def test_deliverable_visibility_for_related_and_unrelated_members(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人与工作项相关成员（含协作者）可见；无关成员 403（16 节）。"""
    ctx = await _setup(client, project)
    alice, bob = ctx["alice"], ctx["bob"]
    item_id = await _create_item(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        str(alice.id),  # type: ignore[union-attr]
        collaborator_ids=[str(bob.id)],  # type: ignore[union-attr]
    )
    await _start_item(client, ctx, item_id)
    resp = await _deliver(
        client, ctx["alice_headers"], item_id, {"type": "text", "content": "说明"}  # type: ignore[arg-type]
    )
    assert resp.status_code == 201

    for role in ("leader_headers", "alice_headers", "bob_headers"):
        resp = await client.get(
            f"/api/v1/work-items/{item_id}/deliverables", headers=ctx[role]  # type: ignore[arg-type]
        )
        assert resp.status_code == 200, f"{role} 应可见"
    denied = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables", headers=ctx["dave_headers"]  # type: ignore[arg-type]
    )
    assert denied.status_code == 403
    denied_single = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables/1", headers=ctx["dave_headers"]  # type: ignore[arg-type]
    )
    assert denied_single.status_code == 403


# ---------- 提交权限与 submit 前置校验 ----------


async def test_non_assignee_cannot_submit_deliverable(
    client: httpx.AsyncClient, project: Project
) -> None:
    """非主执行人（含其他成员与负责人）提交交付物 → 403（7.5 节）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    for role in ("bob_headers", "leader_headers"):
        resp = await _deliver(
            client, ctx[role], item_id, {"type": "text", "content": "越权提交"}  # type: ignore[arg-type]
        )
        assert resp.status_code == 403, f"{role} 不应能提交交付物"


async def test_submit_requires_existing_deliverable(
    client: httpx.AsyncClient, project: Project
) -> None:
    """无交付物时 submit 被拒（4xx 明确提示）；有交付物后 submit 正常（T4.4 验收）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    rejected = await client.post(
        f"/api/v1/work-items/{item_id}/submit",
        json={"version": 3},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "DELIVERABLE_REQUIRED"
    assert "交付物" in rejected.json()["message"]

    resp = await _deliver(
        client, ctx["alice_headers"], item_id, {"type": "text", "content": "交付说明"}  # type: ignore[arg-type]
    )
    assert resp.status_code == 201
    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/submit",
        json={"version": 3},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "IN_REVIEW"


async def test_type_payload_validation(client: httpx.AsyncClient, project: Project) -> None:
    """git_link/text 缺 content、file 缺 file_id → 422。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    for payload in (
        {"type": "git_link"},
        {"type": "text"},
        {"type": "file"},
        {"type": "file", "content": "不应携带"},
    ):
        resp = await _deliver(client, ctx["alice_headers"], item_id, payload)  # type: ignore[arg-type]
        assert resp.status_code == 422, payload


# ---------- file 类型：sha256 追溯与归属 ----------


async def test_file_deliverable_traces_stored_file_sha256(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """file 类型交付物可追溯到 stored_files 的 sha256（T4.4 验收）；
    未关联工作项的文件在提交时建立关联。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    upload = await client.post(
        "/api/v1/files",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
        files={"file": ("result.txt", CONTENT, "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    expected_sha = hashlib.sha256(CONTENT).hexdigest()

    resp = await _deliver(
        client, ctx["alice_headers"], item_id, {"type": "file", "file_id": file_id}  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "file"
    assert body["file"]["sha256"] == expected_sha  # 哈希可追溯
    assert body["file"]["original_filename"] == "result.txt"

    # 上传时未关联工作项的文件，提交交付物后关联到该工作项
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, __import__("uuid").UUID(file_id))
        assert stored is not None
        assert str(stored.work_item_id) == item_id


async def test_file_deliverable_rejects_file_of_other_work_item(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """引用已关联其他工作项的文件 → 422；引用不存在的文件 → 404。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    alice_headers = ctx["alice_headers"]  # type: ignore[assignment]
    item_a = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    item_b = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_a)
    await _start_item(client, ctx, item_b)

    upload = await client.post(
        "/api/v1/files",
        headers=alice_headers,
        files={"file": ("result.txt", CONTENT, "text/plain")},
        data={"work_item_id": item_a},
    )
    file_id = upload.json()["id"]

    resp = await _deliver(client, alice_headers, item_b, {"type": "file", "file_id": file_id})
    assert resp.status_code == 422

    missing = await _deliver(
        client,
        alice_headers,
        item_b,
        {"type": "file", "file_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404


# ---------- 协作回传引用交付物/文件（T4.4） ----------


async def test_collaboration_submit_can_reference_deliverable_and_file(
    client: httpx.AsyncClient, project: Project, storage: LocalStorageProvider
) -> None:
    """协作请求回传可携带 deliverable_id / file_id 引用（校验存在性与归属）。"""
    ctx = await _setup(client, project)
    alice, bob = ctx["alice"], ctx["bob"]
    alice_headers = ctx["alice_headers"]  # type: ignore[assignment]
    bob_headers = ctx["bob_headers"]  # type: ignore[assignment]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    # 主执行人先提交一个交付物版本
    deliverable = await _deliver(
        client, alice_headers, item_id, {"type": "text", "content": "主交付物"}
    )
    deliverable_id = deliverable.json()["id"]

    # 协作接收人上传产物文件（关联本工作项）
    upload = await client.post(
        "/api/v1/files",
        headers=bob_headers,
        files={"file": ("collab.txt", CONTENT, "text/plain")},
        data={"work_item_id": item_id},
    )
    file_id = upload.json()["id"]

    # 发起协作：alice → bob；bob 接受并开始
    created = await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        json={"assignee_id": str(bob.id), "title": "补充数据", "goal": "提供评测数据"},  # type: ignore[union-attr]
        headers=alice_headers,
    )
    assert created.status_code == 201, created.text
    req_id = created.json()["id"]
    accepted = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept", json={"version": 1}, headers=bob_headers
    )
    assert accepted.status_code == 200
    started = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/start", json={"version": 2}, headers=bob_headers
    )
    assert started.status_code == 200

    submitted = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/submit",
        json={
            "version": 3,
            "result_text": "数据已整理",
            "deliverable_id": deliverable_id,
            "file_id": file_id,
        },
        headers=bob_headers,
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "SUBMITTED"
    assert body["result_deliverable_id"] == deliverable_id
    assert body["result_file_id"] == file_id

    # 详情接口可查到引用
    detail = await client.get(
        f"/api/v1/collaboration-requests/{req_id}", headers=alice_headers
    )
    assert detail.json()["result_deliverable_id"] == deliverable_id
    assert detail.json()["result_file_id"] == file_id


async def test_collaboration_submit_rejects_foreign_deliverable(
    client: httpx.AsyncClient, project: Project
) -> None:
    """协作回传引用其他工作项的交付物 → 422。"""
    ctx = await _setup(client, project)
    alice, bob = ctx["alice"], ctx["bob"]
    alice_headers = ctx["alice_headers"]  # type: ignore[assignment]
    bob_headers = ctx["bob_headers"]  # type: ignore[assignment]
    item_a = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    item_b = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_a)
    await _start_item(client, ctx, item_b)

    deliverable = await _deliver(
        client, alice_headers, item_b, {"type": "text", "content": "B 的交付物"}
    )
    foreign_deliverable_id = deliverable.json()["id"]

    created = await client.post(
        f"/api/v1/work-items/{item_a}/collaboration-requests",
        json={"assignee_id": str(bob.id), "title": "补充数据", "goal": "提供评测数据"},  # type: ignore[union-attr]
        headers=alice_headers,
    )
    req_id = created.json()["id"]
    await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept", json={"version": 1}, headers=bob_headers
    )
    await client.post(
        f"/api/v1/collaboration-requests/{req_id}/start", json={"version": 2}, headers=bob_headers
    )
    submitted = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/submit",
        json={"version": 3, "result_text": "x", "deliverable_id": foreign_deliverable_id},
        headers=bob_headers,
    )
    assert submitted.status_code == 422
