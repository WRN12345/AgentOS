"""Dev Doc Review Agent 契约测试（设计文档 2026-07-30 §4.4/§7，mock 模型）。

风格对齐 tests/test_agent_pipeline.py：脚本化替身 Provider 走真实路径
（specialist → call_model_json → build_output → validate_output）。
覆盖：
- 合法输出 → run 成功、建议入库，suggestion_type=dev_doc_review、
  prompt_version=dev_doc_review.v1，content 含 checklist/alignment/verdict，
  fact_refs 引用真实 work_item_id 与 dev_doc_id；
- 载荷校验：缺 verdict / verdict 非法值 / checklist 为空 →
  SuggestionValidationError（schema_validate）；
- 模型非法 JSON → run=failed + json_parse 诊断，不产生建议；
- 护栏：成功 run 不产生业务审计事件、不触碰 dev_docs 业务状态。
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
    """模型替身：返回脚本化 JSON 文本，记录每次调用。"""

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
    """直接建库：READY 工作项（主执行人 alice）+ SUBMITTED 开发文档。"""
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


# ---------- 载荷 Schema 单元契约 ----------


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


# ---------- 端到端契约 ----------


async def test_dev_doc_review_produces_contract_suggestion(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法输出 → run 成功、建议入库且符合 §4.4 契约；护栏：无业务写入。"""
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

            # 护栏：不产生业务审计事件；dev_docs 状态未被 Agent 触碰
            current_audit = set((await session.execute(select(AuditEvent.id))).scalars().all())
            assert current_audit - baseline_audit == set()
            current_doc = await session.get(DevDoc, doc.id)
            assert current_doc is not None and current_doc.status == "SUBMITTED"

        # 模型上下文含文档正文与验收标准（最小上下文）
        assert len(provider.calls) == 1
        assert provider.calls[0]["json_output"] is True
        assert "检索 + 生成" in provider.calls[0]["prompt"]
        assert "评估集准确率" in provider.calls[0]["prompt"]
    finally:
        await redis_client.aclose()


async def test_dev_doc_review_invalid_json_fails_run(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型返回非法 JSON → run=failed + json_parse 诊断，不产生建议（17.3 节）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    item_id, _ = await _make_item_with_doc(leader, alice)

    _patch_provider(monkeypatch, _ScriptedProvider(["{not json"]))
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 一次定终态（17.3 节）

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
