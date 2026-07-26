"""骨架冒烟测试：配置加载与任务封装。"""

from app.core.config import settings
from app.infrastructure.queue.queue import make_task


def test_settings_load() -> None:
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url.startswith("redis://")


def test_make_task_shape() -> None:
    task = make_task("example.ping", {"source": "test"})
    assert task["type"] == "example.ping"
    assert task["payload"] == {"source": "test"}
    assert task["id"]
    assert task["enqueued_at"]
