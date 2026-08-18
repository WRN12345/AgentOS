"""T6.3/T6.4 端到端验收场景共用辅助。

只组合既有资产（conftest fixtures、helpers_t6a/helpers_t6b、领域只读查询），
不修改任何既有文件。提供：
- StubModelProvider / stub_provider fixture：替身模型 Provider（同 test_agent_contract
  的契约思路，独立定义避免测试文件互相 import）；
- 业务状态快照/比对：验证 Agent 运行不改变正式业务状态（18.3 节）；
- 审计/通知只读查询与"按审计事件回放时序"的断言工具。
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.specialists import common as specialist_common
from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.provider import ModelProvider
from tests.helpers_t6b import run_agent_once


class StubModelProvider(ModelProvider):
    """替身 ModelProvider（15 节接口）：set_script 编程输出文本。

    与 test_agent_contract.StubModelProvider 同思路，这里独立定义一份，
    避免端到端场景文件 import 其他测试模块造成耦合。
    """

    name = "stub"
    model = "stub-model"
    is_external = False

    def __init__(self) -> None:
        self._script: str = ""
        self.calls: list[dict[str, Any]] = []

    def set_script(self, script: str) -> None:
        self._script = script

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_output: bool = False,
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_output": json_output})
        return self._script


#: Requirement Analyst 的合法结构化输出（10.2 节 Schema），供替身 Provider 返回
VALID_REQUIREMENT_OUTPUT = json.dumps(
    {
        "content": {
            "summary": "需求应拆为检索与生成两个子目标",
            "rationale": "检索质量是 RAG 的瓶颈",
            "goals": ["完成检索链路", "完成生成链路"],
            "acceptance_criteria": ["评估集准确率 ≥ 80%"],
        },
        "confidence": 0.75,
        "risks": "样本量不足时结论仅供参考",
    },
    ensure_ascii=False,
)


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> StubModelProvider:
    """把替身 Provider 挂到 specialists 的模型入口（场景内 set_script 编程输出）。"""
    provider = StubModelProvider()
    monkeypatch.setattr(specialist_common, "get_model_provider", lambda: provider)
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 一次定终态
    return provider


# ---------- Agent 运行驱动 ----------


async def drive_agent_run(
    work_item_id: str | None = None,
    *,
    project_id: uuid.UUID,
    agent_type: str = "requirement_analyst",
    prompt: str = "端到端场景：请给出分析建议",
) -> AgentRun:
    """创建一次 agent 运行并用替身 Provider 同步驱动到终态（不真起 worker 进程）。

    复用 helpers_t6b.run_agent_once（直接调 worker 处理函数，并清空队列残留）。
    """
    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project_id,
                agent_type=agent_type,
                prompt=prompt,
                work_item_id=uuid.UUID(work_item_id) if work_item_id else None,
            )
        await run_agent_once(redis_client, run.id, prompt)
    finally:
        await redis_client.aclose()

    async with async_session_factory() as session:
        final = await session.get(AgentRun, run.id)
        assert final is not None
        return final


async def get_suggestions_for_run(run_id: uuid.UUID) -> list[AgentSuggestion]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )


# ---------- 业务状态快照（验证 Agent 不污染正式业务状态） ----------


async def snapshot_business_state(item_ids: list[str]) -> dict[str, Any]:
    """正式业务状态快照：工作项 (status, version) + 全部审计事件 id 集合。

    Agent 运行前后各取一次并比对：工作项/审批等正式状态只应由人的操作改变。
    """
    async with async_session_factory() as session:
        items: dict[str, dict[str, Any]] = {}
        for item_id in item_ids:
            row = await session.get(WorkItem, uuid.UUID(item_id))
            assert row is not None
            items[item_id] = {"status": row.status, "version": row.version}
        audit_ids = set((await session.execute(select(AuditEvent.id))).scalars().all())
    return {"work_items": items, "audit_ids": audit_ids}


def assert_business_state_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """18.3 节：Agent 建议不得改变正式业务状态（工作项状态/版本、审计事件）。"""
    assert after["work_items"] == before["work_items"], "Agent 运行不得触碰工作项业务状态"
    new_events = after["audit_ids"] - before["audit_ids"]
    assert new_events == set(), "Agent 运行不得新增业务审计事件"


# ---------- 审计 / 通知只读查询 ----------


async def all_audit_events() -> list[AuditEvent]:
    """全量审计事件，按创建时间正序（同一事务内事件 created_at 相同，保持插入序）。"""
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def notifications_for(member_id: uuid.UUID) -> list[Notification]:
    """某成员的全部站内通知，按创建时间正序。"""
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.recipient_id == member_id)
                    .order_by(Notification.created_at, Notification.id)
                )
            )
            .scalars()
            .all()
        )


def assert_notification(
    notifications: list[Notification],
    *,
    type: str,  # noqa: A002 - 与 Notification.type 字段同名
    title_contains: str | None = None,
) -> Notification:
    """断言通知列表中存在指定类型的通知（可选校验标题关键字）。"""
    matches = [n for n in notifications if n.type == type]
    assert matches, f"缺少类型为 {type} 的通知，实际: {[n.type for n in notifications]}"
    if title_contains is not None:
        assert any(title_contains in n.title for n in matches), (
            f"{type} 通知标题不含「{title_contains}」，实际: {[n.title for n in matches]}"
        )
    return matches[0]


# ---------- 审计回放 ----------


def replay_timeline(events: list[AuditEvent]) -> list[tuple[str, str | None]]:
    """把审计事件折叠为时序步骤：(action, target_id 字符串)。

    同一事务写入的多个事件 created_at 相同（server_default func.now() 取事务时间），
    其相对顺序不纳入断言——调用方比对时按"时间戳分组"处理。
    """
    return [(e.action, str(e.target_id) if e.target_id else None) for e in events]


def assert_audit_replay(
    events: list[AuditEvent],
    expected: list[tuple[str, str | None]],
) -> None:
    """按审计事件回放场景时序并与预期逐步比对。

    事件按 created_at 分组：组间顺序必须严格一致；组内（同一事务）允许任意排列，
    但多重集合必须相等。expected 为完整的预期步骤序列（不多不少）。
    """

    def _group(seq: list[tuple[Any, ...]]) -> list[list[tuple[Any, ...]]]:
        """按（created_at 或占位）把有序序列切成"同刻"分组。"""
        groups: list[list[tuple[Any, ...]]] = []
        keys: list[Any] = []  # 每个分组的时间戳 key（step 里不含 key，需单独记录）
        for row in seq:
            key, step = row[0], row[1:]
            if groups and keys[-1] == key:
                groups[-1].append(step)
            else:
                keys.append(key)
                groups.append([step])
        return groups

    actual_keyed = [(e.created_at, e.action, str(e.target_id) if e.target_id else None) for e in events]
    # 预期序列没有事务时间戳：用"相邻是否同组"无法得知，这里约定——
    # 预期逐步骤默认各占一组；同刻分组仅用于放宽实际侧的组内顺序。
    actual_groups = _group(actual_keyed)

    # 把实际同刻分组展平为"组多重集合"序列，再与预期逐步对齐：
    # 预期步骤数必须等于实际事件数；逐组消费时，组内实际事件可任意匹配
    # 接下来的 len(group) 个预期步骤（多重集合相等即可）。
    assert len(events) == len(expected), (
        f"审计事件数与预期步骤数不一致：实际 {len(events)}，预期 {len(expected)}\n"
        f"实际: {replay_timeline(events)}\n预期: {expected}"
    )
    cursor = 0
    for group in actual_groups:
        size = len(group)
        expected_slice = expected[cursor : cursor + size]
        assert len(expected_slice) == size, "预期步骤不足以匹配实际同刻分组"
        assert sorted(group) == sorted(expected_slice), (
            f"审计回放与预期不符（同刻分组）：实际 {sorted(group)}，预期 {sorted(expected_slice)}\n"
            f"实际时间线: {[(e.action, e.created_at.isoformat()) for e in events]}"
        )
        cursor += size
    assert cursor == len(expected)
