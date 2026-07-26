"""Compose 健康检查命令：python -m app.workers.healthcheck <worker|scheduler>

心跳存在且未过期时退出码为 0，否则为 1。
"""

import asyncio
import sys

from app.infrastructure.cache.redis import create_redis_client
from app.workers.heartbeat import heartbeat_key


async def check(name: str) -> bool:
    redis_client = create_redis_client()
    try:
        return bool(await redis_client.exists(heartbeat_key(name)))
    except Exception:  # noqa: BLE001 - Redis 不可达视为不健康
        return False
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    process_name = sys.argv[1] if len(sys.argv) > 1 else "worker"
    sys.exit(0 if asyncio.run(check(process_name)) else 1)
