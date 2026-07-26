"""Redis 异步客户端封装。"""

import redis.asyncio as redis

from app.core.config import settings


def create_redis_client() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True)
