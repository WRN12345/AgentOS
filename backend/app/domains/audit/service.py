"""审计写入服务（原则 5、第 16 章）。

record_event 与业务状态变更在同一个数据库事务中写入：只 flush 不 commit，
由调用方与业务写一起统一提交 —— 事件写入失败会导致业务写入一并回滚。
request_id、来源 IP 与项目归属从请求上下文（contextvars，中间件自动填充）读取，
业务代码无需手传。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_client_ip, get_project_id, get_request_id
from app.domains.audit.models import AuditEvent

_PROJECT_FROM_CONTEXT = object()


async def record_event(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    project_id: uuid.UUID | None | object = _PROJECT_FROM_CONTEXT,
) -> AuditEvent:
    """追加一条审计事件。只提供追加能力，不提供修改和删除路径。

    默认兼容既有项目接口，从已由门禁校验的请求上下文读取；admin/global
    服务必须显式传目标项目或 None，避免客户端伪造请求头改变审计归属。
    """
    resolved_project_id = (
        get_project_id() if project_id is _PROJECT_FROM_CONTEXT else project_id
    )
    event = AuditEvent(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        request_id=get_request_id() or None,
        source_ip=get_client_ip() or None,
        project_id=resolved_project_id,
    )
    session.add(event)
    await session.flush()  # flush 让写入失败（如约束违反）在 commit 前暴露，保证同生共死
    return event
