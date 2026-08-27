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
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
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
        {"type": "git_link", "content": "https://github.com/org/repo/pull/42"},
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
    assert second.json()["content"] == "https://github.com/org/repo/pull/42"
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


async def test_git_link_is_normalized_when_submitted(
    client: httpx.AsyncClient, project: Project
) -> None:
    """标准 GitHub PR 链接会去除首尾空白和尾部斜杠后保存。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    response = await _deliver(
        client,
        ctx["alice_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "type": "git_link",
            "content": "  https://github.com/org/repo/pull/42/  ",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["content"] == "https://github.com/org/repo/pull/42"


async def test_gitee_pull_request_is_normalized_when_submitted(
    client: httpx.AsyncClient, project: Project
) -> None:
    """Gitee Pull Request 使用同一交付接口并保存规范地址。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    response = await _deliver(
        client,
        ctx["alice_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "type": "git_link",
            "content": "  https://gitee.com/org/repo/pulls/42/  ",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["content"] == "https://gitee.com/org/repo/pulls/42"


async def test_gitlab_merge_request_with_nested_group_is_submitted(
    client: httpx.AsyncClient, project: Project
) -> None:
    """GitLab Merge Request 支持嵌套 group，并移除尾部斜杠。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    response = await _deliver(
        client,
        ctx["alice_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "type": "git_link",
            "content": "https://gitlab.com/group/subgroup/repo/-/merge_requests/42/",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["content"] == (
        "https://gitlab.com/group/subgroup/repo/-/merge_requests/42"
    )


async def test_supported_commit_urls_are_normalized_when_submitted(
    client: httpx.AsyncClient, project: Project
) -> None:
    """三平台 Commit 接受 7–40 位 SHA，并统一保存为小写。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    cases = (
        (
            "https://github.com/org/repo/commit/ABCDEF1/",
            "https://github.com/org/repo/commit/abcdef1",
        ),
        (
            "https://gitee.com/org/repo/commit/0123456789ABCDEF0123456789ABCDEF01234567",
            "https://gitee.com/org/repo/commit/0123456789abcdef0123456789abcdef01234567",
        ),
        (
            "https://gitlab.com/group/subgroup/repo/-/commit/ABC12345",
            "https://gitlab.com/group/subgroup/repo/-/commit/abc12345",
        ),
    )
    for content, expected in cases:
        response = await _deliver(
            client,
            ctx["alice_headers"],  # type: ignore[arg-type]
            item_id,
            {"type": "git_link", "content": content},
        )
        assert response.status_code == 201, response.text
        assert response.json()["content"] == expected


async def test_git_link_rejects_unsupported_urls_without_creating_versions(
    client: httpx.AsyncClient, project: Project
) -> None:
    """Git 链接只接受三平台支持的评审或 Commit URL，失败请求不产生版本。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[arg-type,union-attr]
    await _start_item(client, ctx, item_id)

    invalid_urls = (
        "http://github.com/org/repo/pull/42",
        "https://github.com.evil.example/org/repo/pull/42",
        "https://gitlab.com/org/repo/pull/42",
        "https://github.com/org/repo",
        "https://github.com/org/repo/issues/42",
        "https://github.com/org/repo/pull/42/files",
        "https://github.com/org/repo/pull/42?diff=split",
        "https://github.com/org/repo/pull/42#discussion",
        "https://github.com/org/../pull/42",
        "https://github.com/org/%2e%2e/pull/42",
        "https://github.com/org/foo/../repo/pull/42",
        "https://github.com/org/%ZZ/pull/42",
        "https://user@github.com/org/repo/pull/42",
        "https://github.com:443/org/repo/pull/42",
        "https://github.com/org/repo/pull/0",
        "https://github.com/org/repo/pull/not-a-number",
        "https://github.com/org/repo/commit/abcdef",
        "https://github.com/org/repo/commit/abcdefg",
        "https://github.com/org/repo/commit/12345678901234567890123456789012345678901",
        "https://github.com/org/repo/commit/abcdef1/files",
        "https://gitee.com/org/repo/pulls/42?note=1",
        "https://gitlab.com/group/repo/-/merge_requests/42/diffs",
        "https://gitlab.com/group//repo/-/merge_requests/42",
        "https://gitlab.com/repo/-/commit/abcdef1",
        # NUL/控制字符：必须 422，不能让 PG text 列写入触发 500（spec「非法链接统一返回 422」）
        "https://github.com/org/re\u0000po/pull/42",
        "https://git\thub.com/org/repo/pull/42",
        # 原始 authority 必须精确等于受支持主机：空端口、含控制字符的主机均拒绝，与前端同一规则
        "https://github.com:/org/repo/pull/42",
        # 空 ? / # 分隔符：前端正则拒绝，后端须同规则，避免规则漂移
        "https://github.com/org/repo/pull/42?",
        "https://github.com/org/repo/pull/42#",
        "javascript:alert(1)",
        "not-a-url",
    )
    for content in invalid_urls:
        response = await _deliver(
            client,
            ctx["alice_headers"],  # type: ignore[arg-type]
            item_id,
            {"type": "git_link", "content": content},
        )
        assert response.status_code == 422, content

    history = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert history.status_code == 200
    assert history.json() == []


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


# ---------- GET /deliverables?role=mine（我的交付） ----------


async def test_list_mine_returns_own_deliverables_with_review(
    client: httpx.AsyncClient, project: Project
) -> None:
    """成员看到自己提交的交付物（时间倒序）及审核结论；未审核的 review 为 null；
    他人提交的不出现；匿名 401。"""
    ctx = await _setup(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    # alice 的任务：两个交付物，第 1 版被审核
    item_id = await _create_item(client, leader_headers, str(alice.id))  # type: ignore[arg-type]
    await _start_item(client, ctx, item_id)
    resp = await _deliver(client, alice_headers, item_id, {"type": "text", "content": "第一版"})  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text
    first_id = resp.json()["id"]
    resp = await _deliver(client, alice_headers, item_id, {"type": "text", "content": "第二版"})  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/submit", json={"version": 3}, headers=alice_headers  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    reviewed = await client.post(
        f"/api/v1/work-items/{item_id}/reviews",
        json={"deliverable_id": first_id, "decision": "request_changes", "feedback": "补充测试报告"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert reviewed.status_code == 201, reviewed.text

    # bob 的任务：一个交付物，不应出现在 alice 的列表
    bob_item_id = await _create_item(client, leader_headers, str(bob.id))  # type: ignore[arg-type]
    resp = await client.post(
        f"/api/v1/work-items/{bob_item_id}/publish", json={"version": 1}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{bob_item_id}/dev-doc/waive", json={}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{bob_item_id}/start", json={"version": 2}, headers=ctx["bob_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    resp = await _deliver(client, ctx["bob_headers"], bob_item_id, {"type": "text", "content": "鲍勃的交付"})  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text

    resp = await client.get("/api/v1/deliverables?role=mine", headers=alice_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    mine = resp.json()
    assert len(mine) == 2
    # 时间倒序：第二版在前，未审核 review 为 null
    assert mine[0]["version"] == 2
    assert mine[0]["work_item_title"] == "RAG 工作项"
    assert mine[0]["review"] is None
    # 第一版带审核结论与反馈
    assert mine[1]["version"] == 1
    assert mine[1]["review"]["decision"] == "request_changes"
    assert mine[1]["review"]["feedback"] == "补充测试报告"
    assert mine[1]["review"]["reviewed_by"] == {"id": str(leader.id), "display_name": "负责人"}

    resp = await client.get("/api/v1/deliverables?role=mine")
    assert resp.status_code == 401


# ---------- GET /deliverables（聚合页，可见范围） ----------


async def test_list_visible_scopes_and_feedback_visibility(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人见全部交付物（含反馈）；相关成员（协作者）只见相关工作项且不见
    反馈正文（16 节）；无关成员列表为空；提交人可见自己交付物的反馈。"""
    ctx = await _setup(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    # alice 的任务（bob 为协作者）：提交交付物并被负责人审核（带反馈）
    item_id = await _create_item(
        client, leader_headers, str(alice.id), collaborator_ids=[str(bob.id)]  # type: ignore[arg-type]
    )
    await _start_item(client, ctx, item_id)
    resp = await _deliver(client, alice_headers, item_id, {"type": "text", "content": "交付说明"})  # type: ignore[arg-type]
    assert resp.status_code == 201, resp.text
    deliverable_id = resp.json()["id"]
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/submit", json={"version": 3}, headers=alice_headers  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    reviewed = await client.post(
        f"/api/v1/work-items/{item_id}/reviews",
        json={"deliverable_id": deliverable_id, "decision": "request_changes", "feedback": "补充测试报告"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert reviewed.status_code == 201, reviewed.text

    # 负责人：见全部，含反馈与提交人
    resp = await client.get("/api/v1/deliverables", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["work_item_title"] == "RAG 工作项"
    assert items[0]["submitted_by"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert items[0]["review"]["decision"] == "request_changes"
    assert items[0]["review"]["feedback"] == "补充测试报告"

    # 提交人 alice：见自己任务的交付物，含反馈
    resp = await client.get("/api/v1/deliverables", headers=alice_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["review"]["feedback"] == "补充测试报告"

    # 协作者 bob：相关工作项可见，但反馈正文按 16 节隐藏
    resp = await client.get("/api/v1/deliverables", headers=ctx["bob_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["review"]["decision"] == "request_changes"
    assert items[0]["review"]["feedback"] is None

    # 无关成员 dave：空列表
    resp = await client.get("/api/v1/deliverables", headers=ctx["dave_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert resp.json() == []

    # 匿名 401
    resp = await client.get("/api/v1/deliverables")
    assert resp.status_code == 401
