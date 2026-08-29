"""工作项闭环后的经验总结任务测试。

- 工作项完成（审核通过）触发一次 memory.summary 任务；
- 模型可用时产出经验文字；模型判断无经验（"无"）或工作项未完成 → None；
- 模型不可用静默跳过（ModelError 不抛出、不重试）；其余失败只记日志不重试。
"""

import json
import uuid

import httpx
import pytest

from app.domains.memory import summary as summary_module
from app.domains.memory.summary import summarize_work_item
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.memory_summary import execute_memory_summary
from tests.test_reviews_api import _item_in_review, _review, _setup


class FakeModelProvider(ModelProvider):
    name = "fake"
    model = "fake-llm:v1"
    is_external = False

    def __init__(self, text: str = "这类导入需求上次拆成 4 个工作项，后端占比最大"):
        self._text = text
        self.calls: list[str] = []

    async def generate(
        self, prompt: str, *, system: str | None = None, json_output: bool = False
    ) -> str:
        self.calls.append(prompt)
        return self._text


class UnavailableModelProvider(FakeModelProvider):
    async def generate(self, prompt: str, **kwargs) -> str:  # type: ignore[override]
        raise ModelUnavailableError("model down")


class ExplodingModelProvider(FakeModelProvider):
    async def generate(self, prompt: str, **kwargs) -> str:  # type: ignore[override]
        raise RuntimeError("unexpected")


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


async def _completed_item(client: httpx.AsyncClient, project, ctx: dict | None = None) -> tuple[dict, str]:
    """造一个已完成（审核通过）的工作项，返回 (ctx, item_id)；ctx 可复用避免重复建号。"""
    if ctx is None:
        ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)
    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve", "feedback": "通过"},
    )
    assert resp.status_code == 201, resp.text
    return ctx, item_id


async def test_approve_dispatches_summary_task(
    client: httpx.AsyncClient, project, redis_client
) -> None:
    _, item_id = await _completed_item(client, project)
    queued = [json.loads(t) for t in await redis_client.lrange(QUEUE_KEY, 0, -1)]
    summary_tasks = [t for t in queued if t["type"] == "memory.summary"]
    assert len(summary_tasks) == 1
    assert summary_tasks[0]["payload"]["work_item_id"] == item_id
    assert summary_tasks[0]["payload"]["project_id"] == str(project.id)


async def test_summarize_returns_experience(
    client: httpx.AsyncClient, project, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeModelProvider()
    monkeypatch.setattr(summary_module, "get_model_provider", lambda: provider)
    _, item_id = await _completed_item(client, project)

    async with async_session_factory() as session:
        text = await summarize_work_item(uuid.UUID(item_id), session)
    assert text == "这类导入需求上次拆成 4 个工作项，后端占比最大"
    assert len(provider.calls) == 1
    assert "RAG 工作项" in provider.calls[0]  # 输入为结论文本


async def test_summarize_no_experience_and_unfinished(
    client: httpx.AsyncClient, project, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        summary_module, "get_model_provider", lambda: FakeModelProvider(text="无")
    )
    ctx, item_id = await _completed_item(client, project)
    async with async_session_factory() as session:
        assert await summarize_work_item(uuid.UUID(item_id), session) is None

    unfinished_id, _ = await _item_in_review(client, ctx)
    provider = FakeModelProvider()
    monkeypatch.setattr(summary_module, "get_model_provider", lambda: provider)
    async with async_session_factory() as session:
        assert await summarize_work_item(uuid.UUID(unfinished_id), session) is None
    assert provider.calls == []


async def test_worker_skips_silently_when_model_unavailable(
    client: httpx.AsyncClient, project, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型不可用时任务应静默跳过且不重试。"""
    monkeypatch.setattr(
        summary_module, "get_model_provider", lambda: UnavailableModelProvider()
    )
    _, item_id = await _completed_item(client, project)
    await redis_client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)

    await execute_memory_summary({"work_item_id": item_id})
    assert await redis_client.llen(QUEUE_KEY) == 0
    assert await redis_client.llen(DELAYED_QUEUE_KEY) == 0


async def test_worker_drops_unexpected_failure_without_retry(
    client: httpx.AsyncClient, project, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        summary_module, "get_model_provider", lambda: ExplodingModelProvider()
    )
    _, item_id = await _completed_item(client, project)
    await redis_client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)

    await execute_memory_summary({"work_item_id": item_id})
    assert await redis_client.llen(QUEUE_KEY) == 0


async def test_worker_missing_payload_noop() -> None:
    await execute_memory_summary({})
