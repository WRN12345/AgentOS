"""Worker / Scheduler 存活性信号：心跳写 Redis，供 Compose 健康检查读取。"""

import time

import redis.asyncio as redis

HEARTBEAT_TTL_SECONDS = 30


def heartbeat_key(name: str) -> str:
    return f"agentos:health:{name}"


async def heartbeat(client: redis.Redis, name: str) -> None:
    await client.set(heartbeat_key(name), str(time.time()), ex=HEARTBEAT_TTL_SECONDS)
