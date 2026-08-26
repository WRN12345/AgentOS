"""问答历史测试（2026-08-24 决策修订：按人落库，仅本人可见）。

- 问答后历史落库（answered/refused 均记录，依据/线索做快照）；
- 本人可查（时间倒序）；他人（含负责人）查不到；
- 历史写入失败不影响问答本身（best-effort）。
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.domains.memory import qa as qa_module
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk, QaHistory
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from sqlalchemy import select
from tests.conftest import add_member, auth_headers
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_qa import _ScriptedQAProvider

ALICE_PW = "Alice123!"
LEADER_PW = "Leader123!"


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _seed_chunk(project_id, content: str) -> None:
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type="document",
                source_id=uuid.uuid4(),
                content=content,
                embedding=[0.1] * settings.embedding_dimensions,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


async def test_qa_answer_recorded_and_visible_to_self(
    client: httpx.AsyncClient, project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW, display_name="爱丽丝")
    await _seed_chunk(project_a.id, "发布步骤：先构建镜像")
    monkeypatch.setattr(
        qa_module, "get_model_provider", lambda: _ScriptedQAProvider("先构建镜像 [1]。")
    )
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))

    resp = await client.post(
        "/api/v1/memory/qa", headers=headers, json={"question": "怎么部署"}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/v1/memory/qa/history", headers=headers)
    assert resp.status_code == 200, resp.text
    records = resp.json()
    assert len(records) == 1
    assert records[0]["question"] == "怎么部署"
    assert records[0]["status"] == "answered"
    assert records[0]["answer"] == "先构建镜像 [1]。"
    assert records[0]["sources"][0]["snippet"] == "发布步骤：先构建镜像"


async def test_qa_history_only_visible_to_self(
    client: httpx.AsyncClient, project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负责人也看不到成员的提问历史（仅本人可见）。"""
    _, leader = await add_member(project_a, "leader", LEADER_PW, role="leader")
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    await _seed_chunk(project_a.id, "发布步骤")
    monkeypatch.setattr(
        qa_module, "get_model_provider", lambda: _ScriptedQAProvider("答案 [1]。")
    )
    alice_headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    await client.post("/api/v1/memory/qa", headers=alice_headers, json={"question": "怎么部署"})

    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    resp = await client.get("/api/v1/memory/qa/history", headers=leader_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []  # 负责人看不到 alice 的历史

    async with async_session_factory() as session:
        count = len((await session.execute(select(QaHistory))).scalars().all())
    assert count == 1  # 库里有且仅有 alice 自己那条


async def test_qa_refusal_also_recorded(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    resp = await client.post(
        "/api/v1/memory/qa", headers=headers, json={"question": "没有依据的问题"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refused"

    resp = await client.get("/api/v1/memory/qa/history", headers=headers)
    records = resp.json()
    assert len(records) == 1
    assert records[0]["status"] == "refused"
    assert records[0]["answer"] is None
