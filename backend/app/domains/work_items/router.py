"""工作项接口。

- GET    /work-items                  任何成员：全量列表（assignee_id/status/due 区间过滤）
- POST   /work-items                  仅负责人：创建（初始 DRAFT）
- GET    /work-items/{id}             任何成员：详情
- PATCH  /work-items/{id}             仅负责人：改内容/主执行人/DDL/协作者（携带 version）
- POST   /work-items/{id}/publish     仅负责人：发布 DRAFT → READY
- POST   /work-items/{id}/start       仅主执行人：READY → IN_PROGRESS
- POST   /work-items/{id}/block       仅主执行人：IN_PROGRESS → BLOCKED
- POST   /work-items/{id}/unblock     仅主执行人：BLOCKED → IN_PROGRESS
- POST   /work-items/{id}/submit      仅主执行人：IN_PROGRESS → IN_REVIEW
- POST   /work-items/{id}/cancel      仅负责人：DRAFT/READY/IN_PROGRESS → CANCELLED
所有写接口支持 Idempotency-Key，并要求携带 version 进行乐观锁校验。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.domains.work_items.schemas import (
    WorkItemCommandIn,
    WorkItemCreateIn,
    WorkItemOut,
    WorkItemSummaryOut,
    WorkItemUpdateIn,
)
from app.domains.work_items.service import (
    create_work_item,
    get_work_item,
    list_work_items,
    run_command,
    update_work_item,
    work_item_to_out,
)
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/work-items", tags=["work-items"])


@router.get("", response_model=list[WorkItemSummaryOut])
async def list_work_items_endpoint(
    assignee_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    due_from: datetime | None = Query(default=None),
    due_to: datetime | None = Query(default=None),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[WorkItemSummaryOut]:
    return await list_work_items(
        session,
        project_id=actor.project_id,
        assignee_id=assignee_id,
        status=status,
        due_from=due_from,
        due_to=due_to,
    )


@router.post("", response_model=WorkItemOut, status_code=201)
async def create_work_item_endpoint(
    payload: WorkItemCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> WorkItemOut:
    return await create_work_item(session, actor, payload)


@router.get("/{item_id}", response_model=WorkItemOut)
async def get_work_item_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> WorkItemOut:
    return await work_item_to_out(
        session, await get_work_item(session, item_id, project_id=actor.project_id)
    )


@router.patch("/{item_id}", response_model=WorkItemOut)
async def update_work_item_endpoint(
    item_id: uuid.UUID,
    payload: WorkItemUpdateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> WorkItemOut:
    return await update_work_item(session, actor, item_id, payload)


def _command_endpoint(command: str):  # type: ignore[no-untyped-def]
    """生成统一使用 version 乐观锁和 Idempotency-Key 的状态命令端点。"""

    async def endpoint(
        item_id: uuid.UUID,
        payload: WorkItemCommandIn,
        actor: ProjectMember = Depends(get_current_member),
        _: None = Depends(idempotency_guard),
        session: AsyncSession = Depends(get_session),
    ) -> WorkItemOut:
        return await run_command(session, actor, item_id, command, payload.version)

    return endpoint


for _command in ("publish", "start", "block", "unblock", "submit", "cancel"):
    router.add_api_route(
        f"/{{item_id}}/{_command}",
        _command_endpoint(_command),
        methods=["POST"],
        response_model=WorkItemOut,
        name=f"work_item_{_command}",
    )
