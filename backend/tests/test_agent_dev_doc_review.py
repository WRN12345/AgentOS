"""验证开发文档初审 Agent 的输出契约与业务写入护栏。

合法建议应包含检查清单、对齐说明和结论，并引用真实工作项及文档。非法载荷应
生成诊断；无论成功或失败，Agent 都不得修改开发文档状态或其他业务状态。
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.schemas.suggestion import (
    SuggestionValidationError,
    parse_suggestion_output,
)
from app.agents.service import request_agent_analysis
from app.agents.specialists import dev_doc_review
from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.dev_docs.models import DevDoc
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.workers.worker import handle_task
from tests.conftest import add_member
from tests.test_dev_docs_api import LEADER_PW, ALICE_PW


class _ScriptedProvider:
    """按顺序返回脚本化 JSON 并记录调用参数的模型替身。"""

    name = "scripted"
    model = "scripted-model"
    is_external = False

    def __init__(self, scripts: list[str]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, prompt: str, *, system: str | None = None, json_output: bool = False
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_output": json_output})
        assert self._scripts, "模型调用次数超出脚本长度"
        return self._scripts.pop(0)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _ScriptedProvider) -> None:
    monkeypatch.setattr(
        "app.agents.specialists.common.get_model_provider", lambda: provider
    )


async def _run_once(redis_client, run_id: uuid.UUID) -> None:
    await handle_task(
        {
            "id": str(uuid.uuid4()),
            "type": "agent.run",
            "payload": {"run_id": str(run_id), "prompt": ""},
        },
        redis_client,
    )


def _review_script(verdict: str = "sufficient") -> str:
    return json.dumps(
        {
            "content": {
                "summary": "文档基本完整，可指导开工",
                "rationale": "目标/方案/接口/排期均有覆盖",
                "checklist": [
                    {"aspect": "目标", "verdict": "pass", "note": "目标明确"},
                    {"aspect": "方案", "verdict": "pass", "note": ""},
                    {"aspect": "接口", "verdict": "pass", "note": ""},
                    {"aspect": "排期", "verdict": "uncertain", "note": "未给出里程碑"},
                    {"aspect": "风险", "verdict": "pass", "note": ""},
                ],
                "alignment": "覆盖验收标准第 1、2 条",
                "verdict": verdict,
                "risks": ["排期未细化"],
            },
            "confidence": 0.7,
            "risks": "初审为建议性质，最终由负责人确认",
        },
        ensure_ascii=False,
    )


async def _make_item_with_doc(leader: ProjectMember, alice: ProjectMember) -> tuple[uuid.UUID, DevDoc]:
    """创建待启动工作项及其已提交开发文档。"""
    from app.domains.work_items.models import WorkItem

    async with async_session_factory() as session:
        item = WorkItem(
            title="RAG 工作项",
            description="实现 RAG",
            project_id=leader.project_id,
            assignee_id=alice.id,
            status="READY",
            acceptance_criteria="评估集准确率 ≥ 80%",
        )
        item.collaborators = []
        session.add(item)
        await session.flush()
        doc = DevDoc(
            work_item_id=item.id,
            author_member_id=alice.id,
            content="# 方案\n检索 + 生成",
            status="SUBMITTED",
            doc_version=1,
        )
        session.add(doc)
        await session.commit()
        return item.id, doc


def _valid_payload() -> dict:
    return {
        "suggestion_type": "dev_doc_review",
        "content": {
            "summary": "结论",
            "rationale": "依据",
            "checklist": [{"aspect": "目标", "verdict": "pass", "note": ""}],
            "alignment": "对齐说明",
            "verdict": "sufficient",
            "risks": [],
        },
        "fact_refs": {"work_item_ids": [str(uuid.uuid4())]},
        "confidence": 0.7,
        "risks": "局限",
        "prompt_version": "dev_doc_review.v1",
    }


def test_schema_accepts_valid_dev_doc_review_payload() -> None:
    output = parse_suggestion_output(_valid_payload(), run_id="run-1")
    assert output.suggestion_type == "dev_doc_review"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["content"].update(verdict="approve"),  # 非法 verdict 值
        lambda p: p["content"].update(checklist=[]),  # 空 checklist
        lambda p: p["content"].update(alignment=""),  # 空 alignment
        lambda p: p["content"].update(hacked=True),  # 多余字段
    ],
    ids=["bad_verdict", "empty_checklist", "empty_alignment", "extra_field"],
)
def test_schema_rejects_invalid_dev_doc_review_payload(mutate) -> None:
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(SuggestionValidationError) as exc_info:
        parse_suggestion_output(payload, run_id="run-2")
    assert exc_info.value.diagnostics["stage"] == "schema_validate"


async def test_dev_doc_review_produces_contract_suggestion(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法输出应保存契约完整的建议，且不得产生业务写入。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    item_id, doc = await _make_item_with_doc(leader, alice)

    provider = _ScriptedProvider([_review_script("needs_work")])
    _patch_provider(monkeypatch, provider)

    async with async_session_factory() as session:
        baseline_audit = set((await session.execute(select(AuditEvent.id))).scalars().all())

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type=dev_doc_review.AGENT_TYPE,
                trigger_source="event",
                work_item_id=item_id,
            )
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
            assert suggestion.suggestion_type == dev_doc_review.SUGGESTION_TYPE == "dev_doc_review"
            assert suggestion.prompt_version == dev_doc_review.PROMPT_VERSION == "dev_doc_review.v1"
            content = suggestion.content
            assert content["verdict"] == "needs_work"
            assert [c["aspect"] for c in content["checklist"]] == [
                "目标", "方案", "接口", "排期", "风险",
            ]
            assert content["checklist"][3]["verdict"] == "uncertain"
            assert content["alignment"]
            assert content["risks"] == ["排期未细化"]
            assert suggestion.fact_refs == {
                "work_item_ids": [str(item_id)],
                "dev_doc_ids": [str(doc.id)],
            }

            # 初审 Agent 只能写建议，不能修改文档状态或业务审计流。
            current_audit = set((await session.execute(select(AuditEvent.id))).scalars().all())
            assert current_audit - baseline_audit == set()
            current_doc = await session.get(DevDoc, doc.id)
            assert current_doc is not None and current_doc.status == "SUBMITTED"

        # 模型上下文仅包含初审所需的文档正文和验收标准。
        assert len(provider.calls) == 1
        assert provider.calls[0]["json_output"] is True
        assert "检索 + 生成" in provider.calls[0]["prompt"]
        assert "评估集准确率" in provider.calls[0]["prompt"]
    finally:
        await redis_client.aclose()


async def test_dev_doc_review_invalid_json_fails_run(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 JSON 应产生解析诊断，且不得保存建议。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    item_id, _ = await _make_item_with_doc(leader, alice)

    _patch_provider(monkeypatch, _ScriptedProvider(["{not json"]))
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 隔离自动重试对终态的影响。

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type=dev_doc_review.AGENT_TYPE,
                trigger_source="event",
                work_item_id=item_id,
            )
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "failed"
            diagnostics = json.loads(final.error.split(": ", 1)[1])
            assert diagnostics["stage"] == "json_parse"
            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
            assert suggestion_count == 0
    finally:
        await redis_client.aclose()
