"""知识库问答 API 契约测试（M7.3 验收，设计文档第 11 节②）。

- 命中路径：生成回答 + 依据列表（可定位原文）；
- 拒答路径：低于阈值不生成回答，返回最接近线索（16.13）；
- 权限：仅项目成员（非成员/admin 403）；缺项目头 400。
- 历史落库失败：回答不受影响，日志不含问题/答案/片段等私人内容（16 节）。
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.domains.memory import qa as qa_module
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_qa import _ScriptedQAProvider

ALICE_PW = "Alice123!"


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _seed_chunk(project_id, content: str, *, same_direction: bool) -> None:
    dims = settings.embedding_dimensions
    vec = [0.1] * dims if same_direction else [-0.1] * dims
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type="document",
                source_id=uuid.uuid4(),
                content=content,
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


async def test_qa_answered_path(
    client: httpx.AsyncClient, project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW, display_name="爱丽丝")
    await _seed_chunk(project_a.id, "发布步骤：先构建镜像再滚动重启", same_direction=True)
    monkeypatch.setattr(
        qa_module, "get_model_provider", lambda: _ScriptedQAProvider("发布前先构建镜像 [1]。")
    )

    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    resp = await client.post(
        "/api/v1/memory/qa", headers=headers, json={"question": "怎么部署"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered"
    assert body["answer"] == "发布前先构建镜像 [1]。"
    assert len(body["sources"]) == 1
    assert body["sources"][0]["source_type"] == "document"
    assert "发布步骤" in body["sources"][0]["snippet"]
    assert body["clues"] == []


async def test_qa_refused_path(client: httpx.AsyncClient, project_a: Project) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    await _seed_chunk(project_a.id, "毫不相干的记录", same_direction=False)

    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    resp = await client.post(
        "/api/v1/memory/qa", headers=headers, json={"question": "部署流程是什么"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refused"
    assert body["answer"] is None
    assert len(body["clues"]) == 1  # 最接近的线索供人工判断


async def test_qa_permission(client: httpx.AsyncClient, project_a: Project, admin_user) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    # 非项目成员 → 403
    from tests.conftest import create_admin_user

    outsider = await create_admin_user("outsider", "Out12345!")
    async with async_session_factory() as session:
        from app.domains.identity.models import User

        user = await session.get(User, outsider.id)
        assert user is not None
        user.is_admin = False
        await session.commit()
    headers = await auth_headers(client, "outsider", "Out12345!", project_id=str(project_a.id))
    resp = await client.post("/api/v1/memory/qa", headers=headers, json={"question": "x"})
    assert resp.status_code == 403

    # 全局 admin：问答是生成服务而非内容查看 → 403（只读走检索/列表接口）
    admin_headers = await auth_headers(client, "admin", "Admin123!")
    admin_headers["X-Project-Id"] = str(project_a.id)
    resp = await client.post("/api/v1/memory/qa", headers=admin_headers, json={"question": "x"})
    assert resp.status_code == 403

    # 缺项目头 → 400
    headers = await auth_headers(client, "alice", ALICE_PW)
    resp = await client.post("/api/v1/memory/qa", headers=headers, json={"question": "x"})
    assert resp.status_code == 400


async def test_qa_history_save_failure_logs_no_private_content(
    client: httpx.AsyncClient,
    project_a: Project,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """历史落库失败不影响问答本身；日志只记异常类型——INSERT 参数含
    问题/答案/依据片段等私人内容，异常消息与堆栈不得进日志（16 节日志纪律）。"""
    import logging

    from app.domains.memory import router as memory_router

    _, alice = await add_member(project_a, "alice", ALICE_PW)
    question = "我们的数据库口令是多少"
    await _seed_chunk(project_a.id, "机密片段内容XYZ", same_direction=True)
    monkeypatch.setattr(
        qa_module, "get_model_provider", lambda: _ScriptedQAProvider("机密答案ABC [1]。")
    )

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        # 模拟驱动层异常消息带出 SQL 参数（含私人内容）
        raise RuntimeError("insert failed, params: 我们的数据库口令是多少 / 机密答案ABC")

    monkeypatch.setattr(memory_router, "save_qa_history", _boom)

    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    with caplog.at_level(logging.WARNING):
        resp = await client.post(
            "/api/v1/memory/qa", headers=headers, json={"question": question}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "机密答案ABC [1]。"  # 历史失败不影响回答
    assert "qa history save failed" in caplog.text
    assert question not in caplog.text
    assert "机密答案ABC" not in caplog.text
    assert "机密片段内容XYZ" not in caplog.text
