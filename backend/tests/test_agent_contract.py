"""通过替身 ModelProvider 验证具体 Agent 的结构化输出契约。

系统生成的建议类型、提示词版本和事实引用必须覆盖模型自报值。非法输出、超时和
服务不可用应记录诊断或错误，且不得保存建议、发送通知或污染业务状态。
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.specialists import common as specialist_common
from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.project.models import Project
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider
from tests.helpers_t6b import create_published_item, run_agent_once, setup_trio


class StubModelProvider(ModelProvider):
    """按预设脚本返回文本或抛出异常，并记录每次调用参数。"""

    name = "stub"
    model = "stub-model"
    is_external = False

    def __init__(self, script: Any = "") -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def set_script(self, script: Any) -> None:
        """设置后续调用返回的文本或需要抛出的异常。"""
        self._script = script

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_output: bool = False,
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_output": json_output})
        if isinstance(self._script, Exception):
            raise self._script
        return self._script


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> StubModelProvider:
    """将可编程替身挂载到 specialists 的统一模型入口。"""
    provider = StubModelProvider()
    monkeypatch.setattr(specialist_common, "get_model_provider", lambda: provider)
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 隔离自动重试对终态的影响。
    return provider


async def _start_run(
    redis_client,
    *,
    project_id: uuid.UUID,
    agent_type: str = "requirement_analyst",
    prompt: str = "把这份需求整理成结构化建议",
    work_item_id: uuid.UUID | None = None,
) -> AgentRun:
    async with async_session_factory() as session:
        return await request_agent_analysis(
            session,
            redis_client,
            project_id=project_id,
            agent_type=agent_type,
            prompt=prompt,
            work_item_id=work_item_id,
        )


async def _get_run(run_id: uuid.UUID) -> AgentRun:
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return run


async def _assert_no_business_pollution(
    baseline_audit_ids: set[uuid.UUID], work_item_id: str | None = None
) -> None:
    """确认失败运行未产生建议、通知、审计事件或工作项变更。"""
    async with async_session_factory() as session:
        suggestion_count = (
            await session.execute(select(func.count()).select_from(AgentSuggestion))
        ).scalar_one()
        assert suggestion_count == 0, "失败运行不得保存正式建议"

        notification_count = (
            await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.type == "agent.suggestion_ready")
            )
        ).scalar_one()
        assert notification_count == 0, "失败运行不得发送建议就绪通知"

        current_ids = set((await session.execute(select(AuditEvent.id))).scalars().all())
        new_events = current_ids - baseline_audit_ids
        assert new_events == set(), "Agent 运行不得新增业务审计事件"

        if work_item_id is not None:
            item = await session.get(WorkItem, uuid.UUID(work_item_id))
            assert item is not None
            assert item.version == 2, "Agent 运行不得触碰工作项业务状态"


async def _audit_id_snapshot() -> set[uuid.UUID]:
    """获取运行前的审计事件 ID 基线。"""
    async with async_session_factory() as session:
        return set((await session.execute(select(AuditEvent.id))).scalars().all())


async def test_valid_structured_output_succeeds_with_authoritative_fields(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """合法输出应成功保存，且权威字段必须由系统覆盖。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script(
        json.dumps(
            {
                "content": {
                    "summary": "需求应拆为检索与生成两个子目标",
                    "rationale": "检索质量是 RAG 的瓶颈",
                    "goals": ["完成检索链路", "完成生成链路"],
                    "acceptance_criteria": ["评估集准确率 ≥ 80%"],
                },
                "confidence": 0.75,
                "risks": "样本量不足时结论仅供参考",
                # 模拟模型伪造权威字段，验证系统不会信任这些值。
                "suggestion_type": "HACKED_TYPE",
                "prompt_version": "fake.v999",
                "fact_refs": {"work_item_ids": ["00000000-0000-0000-0000-000000000000"]},
            }
        )
    )

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, project_id=project.id, work_item_id=uuid.UUID(item["id"]))
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "succeeded", final.error

        assert len(stub_provider.calls) == 1
        assert stub_provider.calls[0]["json_output"] is True

        async with async_session_factory() as session:
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
            # 建议身份和事实来源只能由系统确定。
            assert suggestion.suggestion_type == "requirement"
            assert suggestion.prompt_version == "requirement_analyst.v1"
            assert suggestion.fact_refs == {"work_item_ids": [item["id"]]}
            # 非权威的能力扩展内容应原样保留。
            assert suggestion.content["summary"] == "需求应拆为检索与生成两个子目标"
            assert suggestion.content["goals"] == ["完成检索链路", "完成生成链路"]
            assert suggestion.confidence == 0.75
            assert suggestion.risks == "样本量不足时结论仅供参考"

            notification = (
                await session.execute(
                    select(Notification).where(
                        Notification.type == "agent.suggestion_ready",
                        Notification.recipient_id == leader.id,
                    )
                )
            ).scalar_one()
            assert notification is not None
    finally:
        await redis_client.aclose()


async def test_schema_invalid_output_fails_run_with_diagnostics(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """违反 Schema 的输出应保存诊断，且不得污染业务状态。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script(
        json.dumps({"content": {"summary": "只有结论没有理由"}, "confidence": 1.5})
    )

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, project_id=project.id, work_item_id=uuid.UUID(item["id"]))
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        diagnostics = json.loads(final.error.split(": ", 1)[1])
        assert diagnostics["stage"] == "schema_validate"
        assert diagnostics["run_id"] == str(run.id)
        assert diagnostics["errors"]

        await _assert_no_business_pollution(baseline, item["id"])
    finally:
        await redis_client.aclose()


async def test_non_json_output_fails_run_with_json_parse_diagnostics(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """非 JSON 输出应产生解析诊断且不得污染业务状态。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script("我认为这个需求应该先做什么什么……（模型未按要求输出 JSON）")

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, project_id=project.id, work_item_id=uuid.UUID(item["id"]))
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        diagnostics = json.loads(final.error.split(": ", 1)[1])
        assert diagnostics["stage"] == "json_parse"
        assert diagnostics["run_id"] == str(run.id)

        await _assert_no_business_pollution(baseline, item["id"])
    finally:
        await redis_client.aclose()


async def test_json_array_output_fails_run_with_schema_validate(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """JSON 数组不是建议对象，应产生 Schema 诊断。"""
    await setup_trio(client, project)
    stub_provider.set_script('["不是建议对象"]')

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, project_id=project.id)
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        diagnostics = json.loads(final.error.split(": ", 1)[1])
        assert diagnostics["stage"] == "schema_validate"

        await _assert_no_business_pollution(baseline)
    finally:
        await redis_client.aclose()


@pytest.mark.parametrize(
    "error",
    [
        ModelTimeoutError("read timed out"),
        ModelUnavailableError("model service unavailable"),
    ],
    ids=["timeout", "unavailable"],
)
async def test_model_failure_marks_run_failed_without_polluting_business_state(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
    error: Exception,
) -> None:
    """模型超时或不可用时应记录失败和耗时，且不得污染业务状态。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script(error)

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, project_id=project.id, work_item_id=uuid.UUID(item["id"]))
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        assert final.error is not None
        assert final.error.startswith(type(error).__name__)
        assert final.retry_count == 0
        assert final.duration_ms is not None

        await _assert_no_business_pollution(baseline, item["id"])
    finally:
        await redis_client.aclose()
