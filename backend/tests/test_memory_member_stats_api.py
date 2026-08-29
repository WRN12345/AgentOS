"""成员统计查询接口的访问权限与项目隔离测试。

- 项目内成员可查：返回完成数/按时率/负载/样本量标记；
- 非项目成员 403；全局 admin 只读可查；项目隔离（只见本项目成员）。
"""

import httpx

from app.domains.identity.models import User
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers, create_admin_user
from tests.test_memory_member_stats import _add_item

LEADER_PW = "Leader123!"


async def test_member_can_read_stats(client: httpx.AsyncClient, project_a: Project) -> None:
    _, leader = await add_member(project_a, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    await _add_item(project_a, alice, "COMPLETED")
    await _add_item(project_a, alice, "IN_PROGRESS")

    headers = await auth_headers(client, "alice", "Alice123!", project_id=str(project_a.id))
    resp = await client.get("/api/v1/memory/member-stats", headers=headers)
    assert resp.status_code == 200, resp.text
    stats = {s["display_name"]: s for s in resp.json()}
    assert stats["爱丽丝"]["completed_total"] == 1
    assert stats["爱丽丝"]["active_now"] == 1
    assert stats["爱丽丝"]["on_time_rate"] == 1.0  # 无截止视为按时
    assert stats["爱丽丝"]["sample_sufficient"] is False  # n=1 < 5
    assert stats["负责人"]["completed_total"] == 0
    assert stats["负责人"]["on_time_rate"] is None


async def test_non_member_rejected(client: httpx.AsyncClient, project_a: Project) -> None:
    _, leader = await add_member(project_a, "leader", LEADER_PW, role="leader")
    await add_member(project_a, "erin", "Erin12345!")
    headers = await auth_headers(client, "erin", "Erin12345!")  # 不带项目上下文 → 400
    resp = await client.get("/api/v1/memory/member-stats", headers=headers)
    assert resp.status_code == 400

    outsider = await create_admin_user("outsider", "Out12345!")
    async with async_session_factory() as session:
        user = await session.get(User, outsider.id)
        assert user is not None
        user.is_admin = False
        await session.commit()
    headers = await auth_headers(
        client, "outsider", "Out12345!", project_id=str(project_a.id)
    )
    resp = await client.get("/api/v1/memory/member-stats", headers=headers)
    assert resp.status_code == 403


async def test_admin_readonly_and_isolation(
    client: httpx.AsyncClient, project_a: Project, project_b: Project, admin_user
) -> None:
    _, leader = await add_member(project_a, "leader", LEADER_PW, role="leader", display_name="A 负责人")
    await add_member(project_b, "leaderb", "LeaderB123!", role="leader", display_name="B 负责人")

    headers = await auth_headers(client, "admin", "Admin123!")
    headers["X-Project-Id"] = str(project_a.id)
    resp = await client.get("/api/v1/memory/member-stats", headers=headers)
    assert resp.status_code == 200, resp.text
    names = [s["display_name"] for s in resp.json()]
    assert names == ["A 负责人"]  # 只见本项目成员
