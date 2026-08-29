"""审计写入服务。

`record_event` 只执行 `flush`，由调用方与业务变更在同一事务中提交；
事件写入失败时业务变更也会回滚。`request_id`、来源 IP 与项目归属从
基于 `contextvars` 的请求上下文读取，
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

    默认从已经门禁校验的请求上下文读取项目；全局管理服务必须显式传入目标项目
    或 `None`，避免客户端通过伪造请求头改变审计归属。
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
    await session.flush()  # 在 `commit` 前暴露约束错误，确保审计与业务变更共同回滚。
    return event
