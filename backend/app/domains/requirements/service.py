import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.service import record_event
from app.domains.files.service import _validate_type
from app.domains.memory.extractors import SUPPORTED_EXTENSIONS
from app.domains.notifications.service import notify
from app.domains.requirements.extraction import MAX_TEXT, extract_material_text
from app.domains.project.models import Project, ProjectMember
from app.domains.identity.models import User
from app.domains.requirements.models import Analysis, Material, Requirement
from app.domains.requirements.schemas import Content, EditIn, RequirementOut
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.events import publish_after_commit
from app.infrastructure.queue.queue import enqueue
from app.infrastructure.storage.provider import StorageProvider

TASK_TYPE = "requirements.analyze"


async def project_exists(session: AsyncSession, project_id: uuid.UUID) -> None:
    if await session.get(Project, project_id) is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Project not found")


async def audit(session, action, actor_id, resource):
    # Content and citations remain outside the project-visible audit stream.
    await record_event(session, action=f"requirements.{action}", actor_id=actor_id,
                       target_type=resource.__tablename__, target_id=resource.id,
                       project_id=resource.project_id)


async def upload_material(session: AsyncSession, project_id: uuid.UUID, actor: User,
                          upload: UploadFile, provider: StorageProvider) -> Material:
    await project_exists(session, project_id)
    filename = _validate_type((upload.filename or "").replace("\\", "/"), upload.content_type)
    if len(filename) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "Invalid filename")
    if PurePosixPath(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ApiException(415, ErrorCodes.FILE_TYPE_NOT_ALLOWED, "Unsupported material format")
    data = bytearray()
    while chunk := await upload.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > settings.upload_max_bytes:
            raise ApiException(413, ErrorCodes.FILE_TOO_LARGE, "Material exceeds upload limit")
    try:
        text = await extract_material_text(filename, bytes(data))
    except Exception:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "Material text extraction failed") from None
    material_id = uuid.uuid4()
    key = f"admin-materials/{project_id}/{material_id}"
    material = Material(id=material_id, project_id=project_id, uploaded_by=actor.id,
                        original_filename=filename, size_bytes=len(data), storage_key=key,
                        storage_backend=provider.backend_name,
                        chunks=[{"id": str(uuid.uuid4()), "text": text[i:i + 4000]}
                                for i in range(0, len(text), 4000)])
    try:
        await provider.save(key, bytes(data))
        session.add(material)
        await audit(session, "material_uploaded", actor.id, material)
        await session.commit()
    except BaseException:
        await session.rollback()
        await provider.delete(key)
        raise
    return material


async def start_analysis(session: AsyncSession, project_id: uuid.UUID, actor: User,
                         material_ids: list[uuid.UUID]) -> Analysis:
    if await session.scalar(select(Project).where(Project.id == project_id).with_for_update()) is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Project not found")
    ids = set(material_ids)
    materials = list((await session.scalars(select(Material).where(
        Material.project_id == project_id, Material.id.in_(ids)))).all())
    if len(materials) != len(ids):
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Material not found")
    if sum(len(c["text"]) for m in materials for c in m.chunks) > MAX_TEXT:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "Analysis text exceeds 100000 characters")
    selected = sorted(str(i) for i in ids)
    job = await session.scalar(select(Analysis).where(
        Analysis.project_id == project_id, Analysis.material_ids == selected).with_for_update())
    if job is not None and job.status != "failed":
        await session.commit()
        return job
    if job is None:
        job = Analysis(id=uuid.uuid4(), project_id=project_id, requested_by=actor.id,
                       material_ids=selected, available_at=datetime.now(UTC))
        session.add(job)
    else:
        job.status, job.error, job.attempts, job.lease_token = "pending", None, 0, None
        job.requested_by, job.available_at = actor.id, datetime.now(UTC)
    job.next_delivery_at = datetime.now(UTC) + timedelta(seconds=360)
    await audit(session, "analysis_requested", actor.id, job)
    await session.commit()
    client = create_redis_client()
    try:
        await enqueue(client, TASK_TYPE, {"analysis_id": str(job.id)})
    except Exception:
        # Persisted pending jobs are recovered by the worker if enqueue fails.
        job.next_delivery_at = datetime.now(UTC)
        await session.commit()
    finally:
        await client.aclose()
    return job


def requirement_out(item: Requirement, *, leader: bool = False) -> RequirementOut:
    result = RequirementOut.model_validate(item)
    if leader:
        result.sources = []
    return result


async def command(session: AsyncSession, project_id: uuid.UUID, requirement_id: uuid.UUID,
                  actor: User | ProjectMember, version: int, action: str,
                  edit: EditIn | None = None, note: str | None = None,
                  content: Content | None = None) -> RequirementOut:
    stmt = select(Requirement).where(Requirement.id == requirement_id,
                                     Requirement.project_id == project_id)
    leader = isinstance(actor, ProjectMember)
    if leader:
        stmt = stmt.where(Requirement.assignee_id == actor.id,
                          Requirement.status.in_(["dispatched", "accepted", "clarification_requested"]))
    item = await session.scalar(stmt.with_for_update())
    if item is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Requirement not found")
    if leader:
        current = await session.scalar(select(ProjectMember).join(User, User.id == ProjectMember.user_id).where(
            ProjectMember.id == actor.id, ProjectMember.project_id == project_id,
            ProjectMember.role == "leader", ProjectMember.is_active.is_(True), User.is_active.is_(True))
            .with_for_update())
        if current is None:
            raise ApiException(403, ErrorCodes.FORBIDDEN, "Active assigned leader required")
    if item.version != version:
        raise ApiException(409, "REQUIREMENT_VERSION_CONFLICT", "Requirement version changed")
    allowed = {
        "edit": {"draft", "confirmed", "clarification_requested"},
        "confirm": {"draft"}, "exclude": {"draft", "confirmed", "clarification_requested"},
        "dispatch": {"confirmed"}, "accept": {"dispatched"},
        "clarify": {"dispatched", "clarification_requested"},
        "reply": {"draft", "confirmed", "dispatched", "clarification_requested"},
    }
    if item.status not in allowed[action] or leader != (action in {"accept", "clarify"}):
        raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "Requirement status does not allow this command")
    if action in {"reply", "clarify"} and (not note or not note.strip() or len(note) > 10000):
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "A nonempty clarification message is required")
    if action == "edit":
        for key, value in edit.model_dump(exclude={"version"}).items():
            setattr(item, key, value)
        item.status = "draft"
    elif action == "confirm":
        if not item.acceptance_criteria.strip() or item.clarification_questions.strip():
            raise ApiException(422, ErrorCodes.VALIDATION_ERROR,
                               "Acceptance criteria are required and clarification questions must be resolved")
        item.status = "confirmed"
    elif action == "dispatch":
        assignee = await session.scalar(select(ProjectMember).join(User, User.id == ProjectMember.user_id).where(
            ProjectMember.project_id == project_id, ProjectMember.role == "leader",
            ProjectMember.is_active.is_(True), User.is_active.is_(True)).with_for_update())
        if assignee is None:
            raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "Project has no active leader")
        item.assignee_id = assignee.id
        item.leader_note = None
        item.status = "dispatched"
    elif action == "clarify":
        item.leader_note = note.strip()
        item.status = "clarification_requested"
    elif action == "reply":
        if item.assignee_id is None:
            raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "Requirement must be assigned before replying")
        assignee = await session.scalar(select(ProjectMember).join(User, User.id == ProjectMember.user_id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.role == "leader", ProjectMember.is_active.is_(True), User.is_active.is_(True))
            .with_for_update())
        if assignee is None:
            raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "Project has no active leader")
        confirmed_content = content if content is not None else item
        if not confirmed_content.acceptance_criteria.strip() or confirmed_content.clarification_questions.strip():
            raise ApiException(422, ErrorCodes.VALIDATION_ERROR,
                               "Acceptance criteria are required and clarification questions must be resolved")
        if content is not None:
            for key, value in content.model_dump().items():
                setattr(item, key, value)
        item.assignee_id = assignee.id
        item.status, item.leader_note = "dispatched", None
    else:
        item.status = "excluded" if action == "exclude" else "accepted"
    item.version += 1
    if action in {"reply", "clarify"}:
        item.discussion = [*item.discussion, {
            "id": str(uuid.uuid4()), "author_id": str(actor.user_id if leader else actor.id),
            "author_role": "leader" if leader else "admin", "body": note.strip(),
            "created_at": datetime.now(UTC).isoformat(), "version": item.version,
        }]
    outbox = []
    if action in {"dispatch", "reply"}:
        await notify(session, project_id=project_id, recipient_id=item.assignee_id,
                     type="requirements.dispatched" if action == "dispatch" else "requirements.replied",
                     title="Project requirement needs your response",
                     body="A requirement has been assigned or clarified. Open project requirements to respond.",
                     link="/project-requirements", outbox=outbox)
    await audit(session, action, actor.user_id if leader else actor.id, item)
    await session.commit()
    await session.refresh(item)
    await publish_after_commit(outbox)
    return requirement_out(item, leader=leader)
