"""通过 Redis Pub/Sub 按成员频道分发实时事件。

事件生产方横跨多个领域，因此通道实现位于基础设施层，避免领域间相互导入。每个成员
使用独立频道 `agentos:events:{member_id}`，使其他成员的事件不会发送到当前连接，
满足最小暴露原则。

所有事件必须在业务数据库提交成功后发布，确保订阅者收到的事件对应已落库事实。
业务 service 先把事件加入 outbox，提交成功后再统一发布。
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
    """按接收人分发的待发布事件，`type` 与通知类型一致。

    载荷携带 `project_id`，供客户端校验项目归属并隔离前端缓存。
    """

    project_id: uuid.UUID
    recipient_id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None = None


def channel_for(member_id: uuid.UUID) -> str:
    """成员专属频道名。"""
    return f"{CHANNEL_PREFIX}:{member_id}"


def build_payload(event: OutgoingEvent) -> dict[str, Any]:
    """事件载荷：{id, type, project_id, data:{title, body, link}, created_at}。"""
    return {
        "id": str(uuid.uuid4()),
        "type": event.type,
        "project_id": str(event.project_id),
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
    """业务提交成功后，通过短连接发布 outbox 中的事件。

    Redis 暂不可用时仅记录日志，不回滚已提交的业务事实。SSE 不是唯一通道，客户端
    仍可通过 `GET /notifications` 拉取通知。
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
