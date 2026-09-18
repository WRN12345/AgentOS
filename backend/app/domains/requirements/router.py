import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.errors import ApiException, ErrorCodes
from app.domains.files.router import _content_disposition
from app.domains.identity.models import User
from app.domains.project.dependencies import get_current_admin, get_current_leader
from app.domains.project.models import ProjectMember
from app.domains.requirements.models import Analysis, Material, Requirement
from app.domains.requirements.schemas import (
    AnalysisIn, AnalysisOut, ClarifyIn, EditIn, MaterialOut, ReplyIn, RequirementOut, VersionIn,
)
from app.domains.requirements.service import (
    audit, command, project_exists, requirement_out, start_analysis, upload_material,
)
from app.infrastructure.database.engine import get_session
from app.infrastructure.storage.provider import StorageProvider, get_storage_provider

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
    if material is None or material.storage_backend != provider.backend_name or not await provider.exists(material.storage_key):
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
