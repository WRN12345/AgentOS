"""T6.1 Agent 合约测试（10.2、17.3 节）：替身 ModelProvider 验证结构化输出契约。

与既有测试的分工：
- test_model_provider.py：Provider 适配层（httpx.MockTransport，真实 HTTP 语义）；
- test_agent_guardrails.py：Schema 单元契约 + monkeypatch echo 能力的端到端校验；
- test_agent_retry.py：失败退避重试与人工重触发；
- 本文件：六个具体 Agent 走的真实路径（specialists → call_model_json →
  build_output → validate_output），用预编程的替身 ModelProvider（不依赖
  Ollama）验证：
  1. 合法结构化输出 → run 成功、建议落库，且 suggestion_type / prompt_version /
     fact_refs 以系统侧权威值为准（模型自报值被覆盖，10.2 节）；
  2. Schema 非法输出 / 非 JSON 输出 → run=failed + 诊断信息（json_parse /
     schema_validate），不保存正式建议、不发通知（17.3 节）；
  3. 模型超时 / 不可用 → run=failed（重试上限调 0），错误落 agent_runs.error，
     不污染业务状态（工作项、审计、通知均不变，17.3 节"模型失败不回滚/不污染
     已成功的业务动作"）。
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
    """替身 ModelProvider（15 节接口）：按预编程脚本返回文本或抛错。

    - script 为 str：原样作为模型输出返回；
    - script 为 Exception：调用时抛出（模拟超时/不可用）。
    calls 记录每次调用的 prompt/system/json_output，供契约断言。
    """

    name = "stub"
    model = "stub-model"
    is_external = False

    def __init__(self, script: Any = "") -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def set_script(self, script: Any) -> None:
        """编程本次输出：str 原样返回；Exception 调用时抛出。"""
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
    """把替身 Provider 挂到 specialists 的模型入口（脚本由用例内 set_script 编程）。"""
    provider = StubModelProvider()
    monkeypatch.setattr(specialist_common, "get_model_provider", lambda: provider)
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 合约测试一次定终态
    return provider


async def _start_run(
    redis_client,
    *,
    agent_type: str = "requirement_analyst",
    prompt: str = "把这份需求整理成结构化建议",
    work_item_id: uuid.UUID | None = None,
) -> AgentRun:
    async with async_session_factory() as session:
        return await request_agent_analysis(
            session,
            redis_client,
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
    """17.3 节：失败的 Agent 运行不污染业务状态。

    - 无正式建议、无 agent.suggestion_ready 通知；
    - 相对 baseline_audit_ids（运行前的审计事件快照）不新增任何审计事件；
    - 指定工作项存在时其 version 保持 2（create v1 → publish v2，未被触碰）。
    """
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
    """当前全部审计事件 id 快照（Agent 运行前取基线，运行后比对不新增）。"""
    async with async_session_factory() as session:
        return set((await session.execute(select(AuditEvent.id))).scalars().all())


# ---------- 1. 合法结构化输出：成功 + 系统侧权威字段覆盖模型自报值 ----------


async def test_valid_structured_output_succeeds_with_authoritative_fields(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """替身返回合法 JSON → run 成功、建议落库；suggestion_type/prompt_version/fact_refs
    以系统侧为准（模型自报的伪造值被 build_output 覆盖，10.2 节）。"""
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
                # 模型自报的伪造字段：必须被系统侧权威值覆盖
                "suggestion_type": "HACKED_TYPE",
                "prompt_version": "fake.v999",
                "fact_refs": {"work_item_ids": ["00000000-0000-0000-0000-000000000000"]},
            }
        )
    )

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, work_item_id=uuid.UUID(item["id"]))
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "succeeded", final.error

        # 替身被以 json_output=True 调用（结构化输出契约）
        assert len(stub_provider.calls) == 1
        assert stub_provider.calls[0]["json_output"] is True

        async with async_session_factory() as session:
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
            # 系统侧权威字段：不信任模型自报值
            assert suggestion.suggestion_type == "requirement"
            assert suggestion.prompt_version == "requirement_analyst.v1"
            assert suggestion.fact_refs == {"work_item_ids": [item["id"]]}
            # 模型内容保留（含能力自有扩展字段）
            assert suggestion.content["summary"] == "需求应拆为检索与生成两个子目标"
            assert suggestion.content["goals"] == ["完成检索链路", "完成生成链路"]
            assert suggestion.confidence == 0.75
            assert suggestion.risks == "样本量不足时结论仅供参考"

            # 通知负责人查看建议（10.2 节流程末节点）
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


# ---------- 2. 非法输出：拒绝 + 诊断落库，不保存正式建议 ----------


async def test_schema_invalid_output_fails_run_with_diagnostics(
    client: httpx.AsyncClient,
    project: Project,
    stub_provider: StubModelProvider,
) -> None:
    """替身返回缺 rationale 且 confidence 越界的 JSON → run=failed + schema_validate
    诊断；不保存建议、不发通知、不污染业务状态（17.3 节）。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script(
        json.dumps({"content": {"summary": "只有结论没有理由"}, "confidence": 1.5})
    )

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, work_item_id=uuid.UUID(item["id"]))
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
    """替身返回自然语言（非 JSON）→ run=failed + json_parse 诊断（17.3 节解析失败路径）。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script("我认为这个需求应该先做什么什么……（模型未按要求输出 JSON）")

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, work_item_id=uuid.UUID(item["id"]))
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
    """替身返回 JSON 数组（合法 JSON 但非建议对象）→ schema_validate 诊断。"""
    await setup_trio(client, project)
    stub_provider.set_script('["不是建议对象"]')

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client)
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        diagnostics = json.loads(final.error.split(": ", 1)[1])
        assert diagnostics["stage"] == "schema_validate"

        await _assert_no_business_pollution(baseline)
    finally:
        await redis_client.aclose()


# ---------- 3. 模型超时 / 不可用：run=failed，不污染业务状态 ----------


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
    """模型超时/不可用（重试上限调 0）→ run=failed、错误落档、duration 记录，
    且无建议、无通知、工作项业务状态不变（17.3 节）。"""
    ctx = await setup_trio(client, project)
    alice = ctx["alice"]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    stub_provider.set_script(error)

    redis_client = create_redis_client()
    try:
        run = await _start_run(redis_client, work_item_id=uuid.UUID(item["id"]))
        baseline = await _audit_id_snapshot()
        await run_agent_once(redis_client, run.id)

        final = await _get_run(run.id)
        assert final.status == "failed"
        assert final.error is not None
        assert final.error.startswith(type(error).__name__)
        assert final.retry_count == 0  # 上限 0：一次即终态
        assert final.duration_ms is not None

        await _assert_no_business_pollution(baseline, item["id"])
    finally:
        await redis_client.aclose()
