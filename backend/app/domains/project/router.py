"""成员接口（12.2 节）。

- GET  /members                      任何项目成员：全员摘要（含能力、负载）
- POST /members                      负责人：添加已有账号成员（建号收敛到 admin，不建号）
- PATCH /members/{id}                负责人：维护资料、禁用/启用（角色由 admin 指定/变更）
- PUT  /members/{id}/capabilities    本人填报（复位未确认）/ 负责人维护并可确认
权限策略集中在 domains/project/service.py（4.1、6.1 节）。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.domains.project.schemas import (
    CapabilitiesPutIn,
    MemberCreateIn,
    MemberOut,
    MemberUpdateIn,
)
from app.domains.project.service import (
    create_member,
    list_members,
    put_capabilities,
    update_member,
)
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/members", tags=["members"])


@router.get("", response_model=list[MemberOut])
async def list_members_endpoint(
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    return await list_members(session, actor)


@router.post("", response_model=MemberOut, status_code=201)
async def create_member_endpoint(
    payload: MemberCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    return await create_member(session, actor, payload)


@router.patch("/{member_id}", response_model=MemberOut)
async def update_member_endpoint(
    member_id: uuid.UUID,
    payload: MemberUpdateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    return await update_member(session, actor, member_id, payload)


@router.put("/{member_id}/capabilities", response_model=MemberOut)
async def put_capabilities_endpoint(
    member_id: uuid.UUID,
    payload: CapabilitiesPutIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    return await put_capabilities(session, actor, member_id, payload)
