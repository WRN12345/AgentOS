"""两类初审的可信材料快照、项目隔离及约定降级。"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.schemas.suggestion import (
    AgentSuggestionEnvelope,
    parse_suggestion_output,
)
from app.agents.specialists import dev_doc_review, review
from app.agents.tools import TOOL_REGISTRY, list_review_core_memory, write_suggestion
from app.domains.deliverables.models import Deliverable
from app.domains.dev_docs.models import DevDoc
from app.domains.memory.models import CoreMemoryEntry
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member
from tests.test_agent_dev_doc_review import (
    _make_item_with_doc,
    _patch_provider,
    _review_script,
    _ScriptedProvider,
)


@pytest.mark.parametrize("agent_type", ["dev_doc_review", "deliverable_review"])
async def test_review_snapshot_is_authoritative_and_keeps_versions(
    project, leader, monkeypatch, agent_type
):
    item_id, doc = await _make_item_with_doc(leader, leader)
    async with async_session_factory() as session:
        current = await session.get(DevDoc, doc.id)
        current.status = "CONFIRMED"
        current.version += 1
        entry = CoreMemoryEntry(
            project_id=project.id, content="所有接口须带鉴权。\n错误码须明确。",
            confirmed_by_member_id=leader.id,
        )
        old = Deliverable(
            project_id=project.id, work_item_id=item_id, submitted_by=leader.id,
            type="text", content="旧交付材料", version=1,
        )
        latest = Deliverable(
            project_id=project.id, work_item_id=item_id, submitted_by=leader.id,
            type="git_link", content="https://example.test/repo", version=2,
        )
        session.add_all([entry, old, latest])
        await session.commit()
        expected_doc = {"id": str(doc.id), "doc_version": current.doc_version,
                        "version": current.version, "content": current.content}

    payload = json.loads(_review_script())
    payload["content"]["review_context"] = {"dev_doc": {"id": "forged"}}
    payload["fact_refs"] = {"deliverable_ids": ["forged"]}
    provider = _ScriptedProvider([json.dumps(payload)])
    _patch_provider(monkeypatch, provider)
    state = {"work_item_id": str(item_id), "context": {"project": {"id": str(project.id)}}}
    capability = (dev_doc_review.dev_doc_review_capability if agent_type == "dev_doc_review"
                  else review.deliverable_review_capability)
    output = parse_suggestion_output(await capability(state), run_id=str(uuid.uuid4()))
    snapshot = output.content.model_dump(mode="json")["review_context"]
    assert snapshot["dev_doc"] == expected_doc
    assert snapshot["work_item"]["description"] == "实现 RAG"
    assert snapshot["work_item"]["version"] == 1
    assert snapshot["core_memory"] == [{"id": str(entry.id), "content": entry.content}]
    assert snapshot["core_memory_loaded"] is True
    assert output.fact_refs["dev_doc_ids"] == [str(doc.id)]
    assert output.fact_refs["core_memory_ids"] == [str(entry.id)]
    if agent_type == "deliverable_review":
        assert snapshot["deliverable"] == {"id": str(latest.id), "version": 2}
        assert output.fact_refs["deliverable_ids"] == [str(latest.id)]
        assert "旧交付材料" not in provider.calls[0]["prompt"]
    else:
        assert snapshot["deliverable"] is None
    assert entry.content.splitlines()[0] in provider.calls[0]["prompt"]
    assert "数据" in provider.calls[0]["system"]
    assert "人工" in provider.calls[0]["system"]

    async with async_session_factory() as session:
        run = AgentRun(project_id=project.id, work_item_id=item_id, agent_type=agent_type)
        session.add(run)
        await session.flush()
        suggestion = await write_suggestion(
            session, envelope=AgentSuggestionEnvelope(
                **output.model_dump(), run_id=run.id,
            ),
        )
        current = await session.get(DevDoc, doc.id)
        current.content = "更新后的开发文档"
        current.doc_version += 1
        current.version += 1
        current_entry = await session.get(CoreMemoryEntry, entry.id)
        current_entry.content = "更新后的约定"
        await session.commit()
    assert snapshot["dev_doc"] == expected_doc
    assert snapshot["core_memory"][0]["content"] == entry.content
    async with async_session_factory() as session:
        saved = await session.get(AgentSuggestion, suggestion.id)
        assert saved.content["review_context"] == snapshot


@pytest.mark.parametrize("agent_type", ["dev_doc_review", "deliverable_review"])
async def test_core_memory_failure_degrades_review(project, leader, monkeypatch, agent_type):
    item_id, _ = await _make_item_with_doc(leader, leader)
    monkeypatch.setitem(
        TOOL_REGISTRY, "list_review_core_memory",
        replace(TOOL_REGISTRY["list_review_core_memory"], func=AsyncMock(side_effect=RuntimeError("down"))),
    )
    provider = _ScriptedProvider([_review_script()])
    _patch_provider(monkeypatch, provider)
    capability = (dev_doc_review.dev_doc_review_capability if agent_type == "dev_doc_review"
                  else review.deliverable_review_capability)
    result = await capability({
        "work_item_id": str(item_id), "context": {"project": {"id": str(project.id)}}
    })
    output = parse_suggestion_output(result, run_id=str(uuid.uuid4()))
    snapshot = output.content.model_dump()["review_context"]
    assert snapshot["core_memory"] == []
    assert snapshot["core_memory_loaded"] is False
    assert "未参考项目约定" in output.risks
    assert "约定读取失败" in provider.calls[0]["prompt"]
    if agent_type == "deliverable_review":
        assert snapshot["dev_doc"] is None  # SUBMITTED 不是已确认文档
        assert "dev_doc_ids" not in output.fact_refs


async def test_core_memory_only_reads_effective_entries_in_current_project(project, project_b, leader):
    _, other_leader = await add_member(project_b, "other", "password", role="leader")
    async with async_session_factory() as session:
        active = CoreMemoryEntry(
            project_id=project.id, content="当前约定", confirmed_by_member_id=leader.id,
        )
        session.add_all([
            active,
            CoreMemoryEntry(project_id=project.id, content="已作废", status="deprecated",
                            confirmed_by_member_id=leader.id),
            CoreMemoryEntry(project_id=project.id, content="未生效",
                            effective_at=datetime.now(UTC) + timedelta(days=1),
                            confirmed_by_member_id=leader.id),
            CoreMemoryEntry(project_id=project_b.id, content="其他项目秘密",
                            confirmed_by_member_id=other_leader.id),
        ])
        await session.commit()
        assert await list_review_core_memory(session, project_id=project.id) == [
            {"id": str(active.id), "content": "当前约定"}
        ]
        assert await list_review_core_memory(session) == []


@pytest.mark.parametrize("agent_type", ["dev_doc_review", "deliverable_review"])
async def test_reviews_do_not_load_other_project_materials(project_b, leader, monkeypatch, agent_type):
    item_id, _ = await _make_item_with_doc(leader, leader)
    provider = _ScriptedProvider([_review_script()])
    _patch_provider(monkeypatch, provider)
    capability = (dev_doc_review.dev_doc_review_capability if agent_type == "dev_doc_review"
                  else review.deliverable_review_capability)
    output = await capability({
        "work_item_id": str(item_id), "context": {"project": {"id": str(project_b.id)}}
    })
    snapshot = output["content"]["review_context"]
    assert snapshot["work_item"] is None
    assert snapshot["dev_doc"] is None
    assert snapshot["deliverable"] is None
    assert "检索 + 生成" not in provider.calls[0]["prompt"]
