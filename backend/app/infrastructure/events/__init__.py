"""实时事件通道层（4.3 节，T3.6）：Redis Pub/Sub 按成员频道分发。

取舍说明：
- 放在 infrastructure/ 而非 notifications 领域内——Pub/Sub 是与 queue/
  同级的技术机制（Redis 通道），事件生产方横跨 work_items/collaboration/
  transfers/deadlines/worker 多个模块，放基础设施层避免领域间互相 import；
- 按成员分频道（agentos:events:{member_id}）而非单频道+客户端过滤：
  他人事件根本不下发到本连接，符合 16 节最小暴露原则。

发布时机约束：所有 publish 必须发生在业务 DB commit 成功之后
（订阅者收到的事件对应已落库事实），各业务 service 在 notify() 处把事件
追加到 outbox，commit 成功后统一调 publish_events()。
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client

logger = setup_logging("backend")

CHANNEL_PREFIX = "agentos:events"


@dataclass(frozen=True)
class OutgoingEvent:
    """一条待发布事件：与通知同构（type 复用通知 type），按接收人分发。"""

    recipient_id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None = None


def channel_for(member_id: uuid.UUID) -> str:
    """成员专属频道名。"""
    return f"{CHANNEL_PREFIX}:{member_id}"


def build_payload(event: OutgoingEvent) -> dict[str, Any]:
    """事件载荷：{id, type, data:{title, body, link}, created_at}。"""
    return {
        "id": str(uuid.uuid4()),
        "type": event.type,
        "data": {"title": event.title, "body": event.body, "link": event.link},
        "created_at": datetime.now(UTC).isoformat(),
    }


async def publish_events(client: redis.Redis, events: list[OutgoingEvent]) -> None:
    """把事件逐一发布到对应接收人的频道。调用方必须保证业务已 commit。"""
    for event in events:
        await client.publish(
            channel_for(event.recipient_id),
            json.dumps(build_payload(event), ensure_ascii=False),
        )


async def publish_after_commit(events: list[OutgoingEvent]) -> None:
    """业务 commit 成功后调用：自建短连接发布 outbox 中的事件。

    事件发布失败（如 Redis 暂不可用）不影响已提交的业务事实，只记日志——
    客户端仍可通过 GET /notifications 拉取到对应通知（SSE 是增量优化而非唯一通道）。
    """
    if not events:
        return
    client = create_redis_client()
    try:
        await publish_events(client, events)
    except Exception:  # noqa: BLE001 - 发布失败不拖垮已提交的业务请求
        logger.warning("publish events failed, types=%s", [e.type for e in events])
    finally:
        await client.aclose()
