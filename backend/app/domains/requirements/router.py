import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.agents.schemas.analysis import AgentRunOut
from app.agents.service import request_agent_analysis
from app.core.errors import ApiException, ErrorCodes
from app.core.idempotency import idempotency_guard
from app.core.request_context import get_request_id
from app.domains.files.router import _content_disposition
from app.domains.identity.models import User
from app.domains.project.dependencies import get_current_admin, get_current_leader
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.requirements.models import Analysis, Material, Requirement
from app.domains.requirements.schemas import (
    AnalysisIn, AnalysisOut, ClarifyIn, EditIn, MaterialOut, ReplyIn, RequirementOut, VersionIn,
)
from app.domains.requirements.service import (
    audit, command, project_exists, requirement_out, start_analysis, upload_material,
)
from app.infrastructure.database.engine import get_session
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.storage.provider import StorageProvider, get_storage_provider, storage_for

router = APIRouter(tags=["requirements"])
admin = APIRouter(prefix="/admin/projects/{project_id}")


@admin.get("/materials", response_model=list[MaterialOut])
async def materials(project_id: uuid.UUID, actor: User = Depends(get_current_admin),
                    session: AsyncSession = Depends(get_session)):
    await project_exists(session, project_id)
    return (await session.scalars(select(Material).options(defer(Material.chunks)).where(Material.project_id == project_id)
                                 .order_by(Material.created_at.desc(), Material.id))).all()


@admin.post("/materials", response_model=MaterialOut, status_code=201)
async def upload(project_id: uuid.UUID, file: UploadFile = File(...),
                 actor: User = Depends(get_current_admin), session: AsyncSession = Depends(get_session),
                 provider: StorageProvider = Depends(get_storage_provider)):
    return await upload_material(session, project_id, actor, file, provider)


@admin.get("/materials/{material_id}/download")
async def download(project_id: uuid.UUID, material_id: uuid.UUID,
                   actor: User = Depends(get_current_admin), session: AsyncSession = Depends(get_session),
                   provider: StorageProvider = Depends(get_storage_provider)):
    material = await session.scalar(select(Material).options(defer(Material.chunks)).where(
        Material.id == material_id, Material.project_id == project_id))
    if material is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Material not found")
    if material.storage_backend != provider.backend_name:
        provider = storage_for(material.storage_backend)
    if not await provider.exists(material.storage_key):
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Material not found")
    await audit(session, "material_downloaded", actor.id, material)
    await session.commit()
    return StreamingResponse(provider.iter_chunks(material.storage_key), media_type="application/octet-stream",
                             headers={"Content-Disposition": _content_disposition(material.original_filename),
                                      "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@admin.post("/requirement-analyses", response_model=AnalysisOut, status_code=202)
async def analyze(project_id: uuid.UUID, body: AnalysisIn, actor: User = Depends(get_current_admin),
                  session: AsyncSession = Depends(get_session)):
    return await start_analysis(session, project_id, actor, body.material_ids)


@admin.get("/requirement-analyses", response_model=list[AnalysisOut])
async def analyses(project_id: uuid.UUID, actor: User = Depends(get_current_admin),
                   session: AsyncSession = Depends(get_session)):
    await project_exists(session, project_id)
    return (await session.scalars(select(Analysis).where(Analysis.project_id == project_id)
                                 .order_by(Analysis.created_at.desc(), Analysis.id))).all()


@admin.get("/requirements", response_model=list[RequirementOut])
async def requirements(project_id: uuid.UUID, actor: User = Depends(get_current_admin),
                       session: AsyncSession = Depends(get_session)):
    await project_exists(session, project_id)
    return (await session.scalars(select(Requirement).where(Requirement.project_id == project_id)
                                 .order_by(Requirement.created_at, Requirement.id))).all()


@admin.patch("/requirements/{requirement_id}", response_model=RequirementOut)
async def edit(project_id: uuid.UUID, requirement_id: uuid.UUID, body: EditIn,
               actor: User = Depends(get_current_admin), session: AsyncSession = Depends(get_session)):
    return await command(session, project_id, requirement_id, actor, body.version, "edit", edit=body)


@admin.post("/requirements/{requirement_id}/reply", response_model=RequirementOut)
async def reply(project_id: uuid.UUID, requirement_id: uuid.UUID, body: ReplyIn,
                actor: User = Depends(get_current_admin), session: AsyncSession = Depends(get_session)):
    return await command(session, project_id, requirement_id, actor, body.version, "reply",
                         note=body.note, content=body.content)


def admin_command(action: str):
    async def endpoint(project_id: uuid.UUID, requirement_id: uuid.UUID, body: VersionIn,
                       actor: User = Depends(get_current_admin), session: AsyncSession = Depends(get_session)):
        return await command(session, project_id, requirement_id, actor, body.version, action)
    endpoint.__name__ = f"requirement_{action}"
    admin.add_api_route(f"/requirements/{{requirement_id}}/{action}", endpoint,
                        methods=["POST"], response_model=RequirementOut)


for action in ("confirm", "exclude", "dispatch"):
    admin_command(action)

router.include_router(admin)


@router.get("/project-requirements", response_model=list[RequirementOut])
async def assigned(actor: ProjectMember = Depends(get_current_leader), session: AsyncSession = Depends(get_session)):
    items = (await session.scalars(select(Requirement).where(
        Requirement.project_id == actor.project_id, Requirement.assignee_id == actor.id,
        Requirement.status.in_(["dispatched", "accepted", "clarification_requested"]))
        .order_by(Requirement.created_at, Requirement.id))).all()
    return [requirement_out(item, leader=True) for item in items]


@router.post("/project-requirements/{requirement_id}/accept", response_model=RequirementOut)
async def accept(requirement_id: uuid.UUID, body: VersionIn,
                 actor: ProjectMember = Depends(get_current_leader), session: AsyncSession = Depends(get_session)):
    return await command(session, actor.project_id, requirement_id, actor, body.version, "accept")


@router.post("/project-requirements/{requirement_id}/clarify", response_model=RequirementOut)
async def clarify(requirement_id: uuid.UUID, body: ClarifyIn,
                  actor: ProjectMember = Depends(get_current_leader), session: AsyncSession = Depends(get_session)):
    return await command(session, actor.project_id, requirement_id, actor, body.version, "clarify", note=body.note)


@router.post("/project-requirements/{requirement_id}/agent-analysis", response_model=AgentRunOut, status_code=202)
async def decompose(requirement_id: uuid.UUID, body: VersionIn,
                    actor: ProjectMember = Depends(get_current_leader),
                    _: None = Depends(idempotency_guard), session: AsyncSession = Depends(get_session)):
    item = await session.scalar(select(Requirement).where(
        Requirement.id == requirement_id, Requirement.project_id == actor.project_id,
        Requirement.assignee_id == actor.id,
    ).with_for_update())
    if item is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "需求不存在")
    current = await session.scalar(select(ProjectMember).join(User, User.id == ProjectMember.user_id).where(
        ProjectMember.id == actor.id, ProjectMember.project_id == actor.project_id,
        ProjectMember.role == ROLE_LEADER, ProjectMember.is_active.is_(True), User.is_active.is_(True),
    ).with_for_update())
    if current is None:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅当前有效的需求负责人可发起拆解")
    if item.version != body.version:
        raise ApiException(409, "REQUIREMENT_VERSION_CONFLICT", "需求已有新版本，请刷新后重试")
    if item.status != "accepted":
        raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "请先接收该项需求，再进行 AI 拆解")

    # 仅传入需求内容快照，避免字段名和 ID 被 pipeline 误识别为指定人选。
    prompt = "\n\n".join((item.title, item.description, item.acceptance_criteria))
    await audit(session, "decomposition_requested", actor.user_id, item)
    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session, redis_client, agent_type="requirement_pipeline", project_id=actor.project_id,
            prompt=prompt, request_id=get_request_id() or None,
        )
    finally:
        await redis_client.aclose()
    return AgentRunOut.model_validate(run, from_attributes=True)
