import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.handoffs import service
from app.domains.handoffs.schemas import (
    HandoffCreateIn,
    HandoffOut,
    HandoffResponseIn,
    HandoffTargetOut,
)
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["handoffs"])
Actor = Annotated[ProjectMember, Depends(get_current_member)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/handoff-targets", response_model=list[HandoffTargetOut])
async def targets(
    source_work_item_id: uuid.UUID,
    actor: Actor,
    session: Session,
) -> list[HandoffTargetOut]:
    return await service.list_targets(session, actor, source_work_item_id)


@router.get("/work-items/{item_id}/handoffs", response_model=list[HandoffOut])
async def for_work_item(
    item_id: uuid.UUID,
    actor: Actor,
    session: Session,
) -> list[HandoffOut]:
    return await service.list_for_work_item(session, actor, item_id)


@router.get("/handoffs", response_model=list[HandoffOut])
async def inbox(
    actor: Actor,
    session: Session,
    role: Annotated[Literal["received", "sent"], Query()],
    status: Literal["pending", "accepted", "changes_requested"] | None = None,
) -> list[HandoffOut]:
    return await service.list_inbox(session, actor, role, status)


@router.post(
    "/work-items/{item_id}/handoffs", response_model=HandoffOut, status_code=201
)
async def create(
    item_id: uuid.UUID,
    payload: HandoffCreateIn,
    actor: Actor,
    _: Annotated[None, Depends(idempotency_guard)],
    session: Session,
) -> HandoffOut:
    return await service.create_handoff(session, actor, item_id, payload)


@router.post("/handoffs/{handoff_id}/accept", response_model=HandoffOut)
async def accept(
    handoff_id: uuid.UUID,
    payload: HandoffResponseIn,
    actor: Actor,
    _: Annotated[None, Depends(idempotency_guard)],
    session: Session,
) -> HandoffOut:
    return await service.respond(session, actor, handoff_id, payload, accept=True)


@router.post("/handoffs/{handoff_id}/request-changes", response_model=HandoffOut)
async def request_changes(
    handoff_id: uuid.UUID,
    payload: HandoffResponseIn,
    actor: Actor,
    _: Annotated[None, Depends(idempotency_guard)],
    session: Session,
) -> HandoffOut:
    return await service.respond(session, actor, handoff_id, payload, accept=False)
