import asyncio
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.domains.audit.models import AuditEvent
from app.domains.files.models import StoredFile
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import ProjectMember
from app.domains.requirements import service
from app.domains.requirements import extraction
from app.domains.requirements.models import Analysis, Material, Requirement
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.provider import ModelProvider
from app.infrastructure.models.errors import ModelTimeoutError
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from app.workers import requirement_analysis as worker
from tests.conftest import add_member, auth_headers

TEXT = "Customers must export monthly reports as CSV."


class FakeProvider(ModelProvider):
    name, model, is_external = "fake", "fake", False

    def __init__(self):
        self.calls = 0
        self.mutate = lambda result: None
        self.started = None
        self.release = None

    async def generate(self, prompt, *, system=None, json_output=False):
        self.calls += 1
        if self.started:
            self.started.set()
            await self.release.wait()
        material = json.loads(prompt)["materials"][0]
        result = {"requirements": [{
            "title": "CSV export", "description": "Export monthly reports as CSV",
            "acceptance_criteria": "A monthly report can be downloaded as CSV",
            "clarification_questions": "",
            "sources": [{k: material[k] for k in ("material_id", "filename", "chunk_id")} | {
                "quote": material["text"]}],
        }]}
        self.mutate(result)
        return json.dumps(result)


@pytest.fixture
def providers(monkeypatch, tmp_path):
    fake = FakeProvider()
    storage = LocalStorageProvider(tmp_path)
    redis = AsyncMock()
    monkeypatch.setattr(service, "create_redis_client", lambda: redis)
    monkeypatch.setattr(service, "enqueue", AsyncMock())
    monkeypatch.setattr(worker, "get_model_provider", lambda: fake)
    monkeypatch.setattr(worker, "enqueue", AsyncMock())
    app.dependency_overrides[get_storage_provider] = lambda: storage
    yield fake, storage, redis
    app.dependency_overrides.pop(get_storage_provider, None)


async def upload(client, project, headers, filename="contract.txt", data=TEXT):
    response = await client.post(f"/api/v1/admin/projects/{project.id}/materials", headers=headers,
                                 files={"file": (filename, data.encode(), "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


async def analyze(client, project, headers, material):
    response = await client.post(f"/api/v1/admin/projects/{project.id}/requirement-analyses",
                                 headers=headers, json={"material_ids": [material["id"]]})
    assert response.status_code == 202, response.text
    return response.json()


async def generate(client, project, headers):
    material = await upload(client, project, headers)
    job = await analyze(client, project, headers, material)
    await worker.execute_requirement_analysis({"analysis_id": job["id"]}, AsyncMock())
    response = await client.get(f"/api/v1/admin/projects/{project.id}/requirements", headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    return response.json()[0], material, job


@pytest.mark.parametrize("prefix,suffix", [
    ("<think>Check the supplied evidence.</think>\n", ""),
    ("```json\n", "\n```"),
    ("<think>Check the supplied evidence.</think>\n```json\n", "\n```"),
])
async def test_reasoning_and_fences_preserve_valid_requirements(
    client, project_a, admin_headers, providers, monkeypatch, prefix, suffix,
):
    fake, _, redis = providers
    original = fake.generate

    async def wrapped(*args, **kwargs):
        return prefix + await original(*args, **kwargs) + suffix

    monkeypatch.setattr(fake, "generate", wrapped)
    material = await upload(client, project_a, admin_headers)
    job = await analyze(client, project_a, admin_headers, material)
    await worker.execute_requirement_analysis({"analysis_id": job["id"]}, redis)
    async with async_session_factory() as session:
        assert (await session.get(Analysis, uuid.UUID(job["id"]))).status == "succeeded"
        candidate = await session.scalar(select(Requirement))
        assert candidate.sources[0]["quote"] == TEXT
        assert candidate.status == "draft"


@pytest.mark.parametrize("failure,expected", [
    ("schema", "not valid requirement JSON"),
    ("model", "model service unavailable"),
    ("timeout", "model request timed out"),
])
async def test_analysis_failure_reports_stage_without_private_content(
    client, project_a, admin_headers, providers, monkeypatch, caplog, failure, expected,
):
    fake, _, redis = providers
    marker = "sensitive-model-content"
    generate = AsyncMock(return_value=f"<think>{marker}</think>invalid JSON")
    if failure == "model":
        generate.side_effect = RuntimeError(marker)
    elif failure == "timeout":
        generate.side_effect = ModelTimeoutError(marker)
    monkeypatch.setattr(fake, "generate", generate)
    material = await upload(client, project_a, admin_headers)
    job = await analyze(client, project_a, admin_headers, material)
    async with async_session_factory() as session:
        await session.execute(update(Analysis).where(Analysis.id == uuid.UUID(job["id"])).values(attempts=2))
        await session.commit()
    await worker.execute_requirement_analysis({"analysis_id": job["id"]}, redis)
    async with async_session_factory() as session:
        analysis = await session.get(Analysis, uuid.UUID(job["id"]))
        assert analysis.status == "failed"
        assert expected in analysis.error
        assert marker not in analysis.error
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 0
    assert marker not in caplog.text
    assert "stage=" in caplog.text


async def test_material_isolation(client, project_a, project_b, leader, admin_headers, providers):
    material = await upload(client, project_a, admin_headers)
    base = f"/api/v1/admin/projects/{project_a.id}"
    lh = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    await add_member(project_a, "member", "Member123!")
    mh = await auth_headers(client, "member", "Member123!", project_id=str(project_a.id))
    for headers in (lh, mh):
        for path in ("materials", "requirements", "requirement-analyses", f"materials/{material['id']}/download"):
            assert (await client.get(f"{base}/{path}", headers=headers)).status_code == 403
        assert (await client.post(f"{base}/materials", headers=headers,
                                  files={"file": ("a.txt", b"private", "text/plain")})).status_code == 403
    assert (await client.get("/api/v1/project-requirements", headers=mh)).status_code == 403
    assert (await client.get(f"{base}/materials/{material['id']}/download", headers=admin_headers)).text == TEXT
    assert (await client.get(f"/api/v1/admin/projects/{project_b.id}/materials/{material['id']}/download",
                             headers=admin_headers)).status_code == 404
    assert (await client.get(f"/api/v1/files/{material['id']}/download", headers=lh)).status_code == 404
    response = await client.post(f"/api/v1/admin/projects/{project_b.id}/requirement-analyses",
                                 headers=admin_headers, json={"material_ids": [material["id"]]})
    assert response.status_code == 404
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(StoredFile)) == 0
        assert await session.scalar(select(func.count()).select_from(MemoryChunk)) == 0
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(update(Material).values(chunks=[]))


async def test_full_workflow_and_redaction(client, project_a, leader, admin_headers, providers):
    req, material, job = await generate(client, project_a, admin_headers)
    assert req["status"] == "draft" and req["version"] == 1
    assert req["sources"][0]["quote"] == TEXT
    base = f"/api/v1/admin/projects/{project_a.id}/requirements/{req['id']}"
    lh = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    assert (await client.get("/api/v1/project-requirements", headers=lh)).json() == []
    for action, version in (("confirm", 1), ("dispatch", 2)):
        response = await client.post(f"{base}/{action}", headers=admin_headers, json={"version": version})
        assert response.status_code == 200, response.text
    assert response.json()["assignee_id"] == str(leader.id)
    assert (await client.post(f"{base}/exclude", headers=admin_headers, json={"version": 3})).status_code == 409
    items = (await client.get("/api/v1/project-requirements", headers=lh)).json()
    assert items[0]["sources"] == []
    leader_base = f"/api/v1/project-requirements/{req['id']}"
    response = await client.post(f"{leader_base}/clarify", headers=lh, json={"version": 3, "note": "Which timezone?"})
    assert response.status_code == 200 and response.json()["sources"] == []
    edited = {k: req[k] for k in ("title", "description", "acceptance_criteria", "clarification_questions")}
    edited["version"] = 4
    response = await client.patch(base, headers=admin_headers, json=edited)
    assert response.status_code == 200 and response.json()["status"] == "draft"
    assert (await client.get("/api/v1/project-requirements", headers=lh)).json() == []
    for action, version in (("confirm", 5), ("dispatch", 6)):
        assert (await client.post(f"{base}/{action}", headers=admin_headers, json={"version": version})).status_code == 200
    response = await client.post(f"{leader_base}/accept", headers=lh, json={"version": 7})
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    assert response.json()["sources"] == []
    edited["version"] = 8
    assert (await client.patch(base, headers=admin_headers, json=edited)).status_code == 409
    assert (await client.post(f"{leader_base}/accept", headers=lh, json={"version": 7})).status_code == 409
    assert (await analyze(client, project_a, admin_headers, material))["id"] == job["id"]
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 1
        assert await session.scalar(select(func.count()).select_from(WorkItem)) == 0
        events = (await session.scalars(select(AuditEvent).where(AuditEvent.action.like("requirements.%")))).all()
        assert len(events) >= 10
        assert all(e.project_id == project_a.id for e in events)
        assert all(e.before is None and e.after is None for e in events)


@pytest.mark.parametrize("field,value", [("quote", "fabricated"), ("quote", " "),
    ("chunk_id", "unknown"), ("material_id", str(uuid.uuid4())), ("filename", "other.txt")])
async def test_invalid_citations_retry_without_candidates(client, project_a, admin_headers, providers, field, value):
    fake, _, redis = providers
    fake.mutate = lambda result: result["requirements"][0]["sources"][0].update({field: value})
    material = await upload(client, project_a, admin_headers)
    job = await analyze(client, project_a, admin_headers, material)
    for attempt in range(3):
        async with async_session_factory() as session:
            await session.execute(update(Analysis).values(available_at=datetime.now(UTC) - timedelta(seconds=1)))
            await session.commit()
        await worker.execute_requirement_analysis({"analysis_id": job["id"]}, redis)
    async with async_session_factory() as session:
        current = await session.get(Analysis, uuid.UUID(job["id"]))
        assert current.status == "failed" and current.attempts == 3
        assert "source citations" in current.error
        assert "fabricated" not in current.error
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 0
    fake.mutate = lambda result: None
    retry = await analyze(client, project_a, admin_headers, material)
    assert retry["id"] == job["id"] and retry["status"] == "pending"
    await worker.execute_requirement_analysis({"analysis_id": job["id"]}, redis)
    async with async_session_factory() as session:
        assert (await session.get(Analysis, uuid.UUID(job["id"]))).status == "succeeded"


async def test_queue_failure_recovery_and_duplicate_delivery(client, project_a, admin_headers, providers, monkeypatch):
    fake, _, redis = providers
    monkeypatch.setattr(service, "enqueue", AsyncMock(side_effect=ConnectionError))
    material = await upload(client, project_a, admin_headers)
    jobs = await asyncio.gather(*(analyze(client, project_a, admin_headers, material) for _ in range(2)))
    assert jobs[0]["id"] == jobs[1]["id"]
    await worker.recover_requirement_analyses(redis)
    worker.enqueue.assert_awaited_once()
    payload = {"analysis_id": jobs[0]["id"]}
    fake.started, fake.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(worker.execute_requirement_analysis(payload, redis))
    await asyncio.wait_for(fake.started.wait(), 5)
    await worker.execute_requirement_analysis(payload, redis)
    fake.release.set()
    await task
    await worker.execute_requirement_analysis(payload, redis)
    assert fake.calls == 1
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 1


async def test_expired_worker_cannot_publish(client, project_a, admin_headers, providers):
    fake, _, redis = providers
    material = await upload(client, project_a, admin_headers)
    job = await analyze(client, project_a, admin_headers, material)
    payload = {"analysis_id": job["id"]}
    fake.started, fake.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(worker.execute_requirement_analysis(payload, redis))
    await asyncio.wait_for(fake.started.wait(), 5)
    async with async_session_factory() as session:
        await session.execute(update(Analysis).values(available_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    release = fake.release
    fake.started = None
    await worker.execute_requirement_analysis(payload, redis)
    release.set()
    await task
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 1
        assert (await session.get(Analysis, uuid.UUID(job["id"]))).attempts == 2


async def test_version_races_and_confirmation_rules(client, project_a, leader, admin_headers, providers):
    req, _, _ = await generate(client, project_a, admin_headers)
    base = f"/api/v1/admin/projects/{project_a.id}/requirements/{req['id']}"
    edit = {k: req[k] for k in ("title", "description", "acceptance_criteria", "clarification_questions")}
    edit.update(version=1, clarification_questions="Which month?")
    assert (await client.patch(base, headers=admin_headers, json=edit)).status_code == 200
    assert (await client.post(f"{base}/confirm", headers=admin_headers, json={"version": 2})).status_code == 422
    edit.update(version=2, clarification_questions="", acceptance_criteria=" ")
    assert (await client.patch(base, headers=admin_headers, json=edit)).status_code == 200
    assert (await client.post(f"{base}/confirm", headers=admin_headers, json={"version": 3})).status_code == 422
    edit.update(version=3, acceptance_criteria=req["acceptance_criteria"])
    assert (await client.patch(base, headers=admin_headers, json=edit)).status_code == 200
    results = await asyncio.gather(*(client.post(f"{base}/{action}", headers=admin_headers,
        json={"version": 4}) for action in ("confirm", "exclude")))
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_leader_scope_replacement_and_decision_race(client, project_a, project_b, leader, admin_headers, providers):
    req, _, _ = await generate(client, project_a, admin_headers)
    base = f"/api/v1/admin/projects/{project_a.id}/requirements/{req['id']}"
    for action, version in (("confirm", 1), ("dispatch", 2)):
        assert (await client.post(f"{base}/{action}", headers=admin_headers, json={"version": version})).status_code == 200
    await add_member(project_b, "other", "Other123!", role="leader")
    other = await auth_headers(client, "other", "Other123!", project_id=str(project_b.id))
    path = f"/api/v1/project-requirements/{req['id']}"
    assert (await client.post(f"{path}/accept", headers=other, json={"version": 3})).status_code == 404
    lh = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    results = await asyncio.gather(
        client.post(f"{path}/accept", headers=lh, json={"version": 3}),
        client.post(f"{path}/clarify", headers=lh, json={"version": 3, "note": "Need detail"}))
    assert sorted(r.status_code for r in results) == [200, 409]
    async with async_session_factory() as session:
        await session.execute(update(ProjectMember).where(ProjectMember.id == leader.id).values(role="member"))
        await session.commit()
    await add_member(project_a, "replacement", "Other123!", role="leader")
    replacement = await auth_headers(client, "replacement", "Other123!", project_id=str(project_a.id))
    assert (await client.get("/api/v1/project-requirements", headers=replacement)).json() == []
    assert (await client.post(f"{path}/accept", headers=replacement, json={"version": 4})).status_code == 404
    assert (await client.get("/api/v1/project-requirements", headers=lh)).status_code == 403


async def test_admin_path_owns_audit_and_missing_leader(client, project_a, project_b, admin_headers, providers):
    headers = admin_headers | {"X-Project-Id": str(project_b.id)}
    req, _, _ = await generate(client, project_a, headers)
    base = f"/api/v1/admin/projects/{project_a.id}/requirements/{req['id']}"
    assert (await client.post(f"{base}/confirm", headers=headers, json={"version": 1})).status_code == 200
    assert (await client.post(f"{base}/dispatch", headers=headers, json={"version": 2})).status_code == 409
    async with async_session_factory() as session:
        events = (await session.scalars(select(AuditEvent).where(AuditEvent.action.like("requirements.%")))).all()
        assert all(e.project_id == project_a.id for e in events)


async def test_empty_candidates_succeed(client, project_a, admin_headers, providers):
    fake, _, redis = providers
    fake.mutate = lambda result: result.update(requirements=[])
    material = await upload(client, project_a, admin_headers)
    job = await analyze(client, project_a, admin_headers, material)
    await worker.execute_requirement_analysis({"analysis_id": job["id"]}, redis)
    async with async_session_factory() as session:
        assert (await session.get(Analysis, uuid.UUID(job["id"]))).status == "succeeded"
        assert await session.scalar(select(func.count()).select_from(Requirement)) == 0


async def test_recovery_reservation_fairness(project_a, admin_user, providers):
    _, _, redis = providers
    past = datetime.now(UTC) - timedelta(minutes=10)
    async with async_session_factory() as session:
        for _ in range(105):
            session.add(Analysis(project_id=project_a.id, requested_by=admin_user.id,
                material_ids=[str(uuid.uuid4())], available_at=past, next_delivery_at=past))
        await session.commit()
    await worker.recover_requirement_analyses(redis)
    assert worker.enqueue.await_count == 100
    await worker.recover_requirement_analyses(redis)
    assert worker.enqueue.await_count == 105
    await worker.recover_requirement_analyses(redis)
    assert worker.enqueue.await_count == 105
    async with async_session_factory() as session:
        await session.execute(update(Analysis).values(next_delivery_at=past))
        await session.commit()
    await worker.recover_requirement_analyses(redis)
    assert worker.enqueue.await_count == 205


async def test_recovery_of_exhausted_worker(project_a, admin_user, providers):
    _, _, redis = providers
    past = datetime.now(UTC) - timedelta(minutes=10)
    async with async_session_factory() as session:
        job = Analysis(project_id=project_a.id, requested_by=admin_user.id, attempts=3,
                       status="running", lease_token=uuid.uuid4(), material_ids=[],
                       available_at=past, next_delivery_at=past)
        session.add(job)
        await session.commit()
        job_id = job.id
    await worker.recover_requirement_analyses(redis)
    worker.enqueue.assert_not_awaited()
    async with async_session_factory() as session:
        job = await session.get(Analysis, job_id)
        assert job.status == "failed" and job.lease_token is None


async def test_safe_docx_extraction():
    from docx import Document

    document = Document()
    document.add_paragraph(TEXT)
    buffer = io.BytesIO()
    document.save(buffer)
    assert await extraction.extract_material_text("contract.docx", buffer.getvalue()) == TEXT


async def test_compressed_oversized_document_rejected_before_parsing(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (extraction.MAX_ARCHIVE_BYTES + 1))
    spawn = AsyncMock()
    monkeypatch.setattr(extraction.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError, match="Expanded document"):
        await extraction.extract_material_text("contract.docx", buffer.getvalue())
    spawn.assert_not_awaited()


async def test_document_timeout_kills_parser(monkeypatch):
    process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=TimeoutError),
                              kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(extraction.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await extraction.extract_material_text("contract.pdf", b"%PDF-1.4")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_invalid_upload_is_not_stored(client, project_a, admin_headers, providers):
    for content in (b"", b"x" * (extraction.MAX_TEXT + 1)):
        response = await client.post(f"/api/v1/admin/projects/{project_a.id}/materials",
            headers=admin_headers, files={"file": ("contract.txt", content, "text/plain")})
        assert response.status_code == 422
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Material)) == 0
