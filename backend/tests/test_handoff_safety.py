"""移交与既有任务写入口的互斥，以及精确文件版本授权。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.domains.deliverables.models import Deliverable
from app.domains.dev_docs.models import DevDoc
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from tests.conftest import add_member, auth_headers


@pytest.fixture
async def handoff_ctx(client, project):
    members = {}
    headers = {}
    for name in ("leader", "alice", "bob", "carol"):
        _, member = await add_member(
            project, name, "Test12345!", role="leader" if name == "leader" else "member",
        )
        members[name] = member
        headers[name] = await auth_headers(client, name, "Test12345!", project_id=str(project.id))
    async with async_session_factory() as session:
        source = WorkItem(
            project_id=project.id, title="接口开发", assignee_id=members["alice"].id,
            status="IN_PROGRESS",
        )
        target = WorkItem(
            project_id=project.id, title="前端联调", assignee_id=members["bob"].id,
            status="READY",
        )
        session.add_all([source, target])
        await session.flush()
        delivery = Deliverable(
            project_id=project.id, work_item_id=source.id, type="text", content="接口说明",
            version=1, submitted_by=members["alice"].id,
        )
        session.add_all([
            delivery,
            DevDoc(work_item_id=target.id, author_member_id=members["bob"].id, content="", waived=True),
        ])
        await session.commit()
    return SimpleNamespace(source=source, target=target, delivery=delivery, members=members, headers=headers)


def _payload(ctx):
    return {
        "version": ctx.source.version,
        "target_work_item_id": str(ctx.target.id),
        "target_version": ctx.target.version,
        "deliverable_id": str(ctx.delivery.id),
    }


async def _send(client, ctx, **overrides):
    response = await client.post(
        f"/api/v1/work-items/{ctx.source.id}/handoffs",
        headers=ctx.headers["alice"], json={**_payload(ctx), **overrides},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_pending_handoff_protects_both_tasks(client, handoff_ctx):
    ctx = handoff_ctx
    await _send(client, ctx)
    for item, actor, version in ((ctx.source, "alice", 2), (ctx.target, "bob", 1)):
        base = f"/api/v1/work-items/{item.id}"
        edited = await client.patch(
            base, headers=ctx.headers["leader"],
            json={"version": version, "assignee_id": str(ctx.members["carol"].id)},
        )
        assert edited.status_code == 409, edited.text
        cancelled = await client.post(
            f"{base}/cancel", headers=ctx.headers["leader"], json={"version": version},
        )
        assert cancelled.status_code == 409, cancelled.text
        transfer = await client.post(
            f"{base}/transfer-requests", headers=ctx.headers[actor],
            json={"to_member_id": str(ctx.members["carol"].id), "reason": "转交处理", "impact_note": "保持交付范围"},
        )
        assert transfer.status_code == 409, transfer.text
    started = await client.post(
        f"/api/v1/work-items/{ctx.target.id}/start", headers=ctx.headers["bob"], json={"version": 1},
    )
    assert started.status_code == 409, started.text
    delivered = await client.post(
        f"/api/v1/work-items/{ctx.source.id}/deliverables", headers=ctx.headers["alice"],
        json={"type": "text", "content": "替换待接收内容"},
    )
    assert delivered.status_code == 409, delivered.text
    reviewed = await client.post(
        f"/api/v1/work-items/{ctx.source.id}/reviews", headers=ctx.headers["leader"],
        json={"deliverable_id": str(ctx.delivery.id), "decision": "approve"},
    )
    assert reviewed.status_code == 409, reviewed.text


@pytest.mark.parametrize("side,actor", [("source", "alice"), ("target", "bob")])
async def test_preexisting_transfer_cannot_bypass_pending_handoff(client, handoff_ctx, side, actor):
    ctx = handoff_ctx
    item = getattr(ctx, side)
    transfer = await client.post(
        f"/api/v1/work-items/{item.id}/transfer-requests", headers=ctx.headers[actor],
        json={"to_member_id": str(ctx.members["carol"].id), "reason": "转交处理", "impact_note": "保持交付范围"},
    )
    assert transfer.status_code == 201, transfer.text
    await _send(client, ctx)
    approved = await client.post(
        f"/api/v1/transfer-requests/{transfer.json()['id']}/approve",
        headers=ctx.headers["leader"], json={"version": transfer.json()["version"]},
    )
    assert approved.status_code == 409, approved.text


async def test_concurrent_accept_completes_only_once(client, handoff_ctx):
    ctx = handoff_ctx
    handoff = await _send(client, ctx)
    results = await asyncio.gather(*[
        client.post(
            f"/api/v1/handoffs/{handoff['id']}/accept", headers=ctx.headers["bob"],
            json={"version": handoff["version"]},
        ) for _ in range(2)
    ])
    assert sorted(r.status_code for r in results) == [200, 409]
    source = await client.get(f"/api/v1/work-items/{ctx.source.id}", headers=ctx.headers["alice"])
    target = await client.get(f"/api/v1/work-items/{ctx.target.id}", headers=ctx.headers["bob"])
    assert source.json()["status"] == "COMPLETED"
    assert source.json()["version"] == 3
    assert target.json()["status"] == "READY"
    started = await client.post(
        f"/api/v1/work-items/{ctx.target.id}/start", headers=ctx.headers["bob"],
        json={"version": target.json()["version"]},
    )
    assert started.status_code == 200, started.text


async def test_send_and_target_start_are_serialized(client, handoff_ctx):
    ctx = handoff_ctx
    sent, started = await asyncio.gather(
        client.post(
            f"/api/v1/work-items/{ctx.source.id}/handoffs", headers=ctx.headers["alice"], json=_payload(ctx),
        ),
        client.post(
            f"/api/v1/work-items/{ctx.target.id}/start", headers=ctx.headers["bob"], json={"version": 1},
        ),
    )
    assert (sent.status_code, started.status_code) in ((201, 409), (409, 200)), (sent.text, started.text)


async def test_revision_cannot_bypass_recipient_via_legacy_review(client, handoff_ctx):
    ctx = handoff_ctx
    handoff = await _send(client, ctx)
    revised = await client.post(
        f"/api/v1/handoffs/{handoff['id']}/request-changes", headers=ctx.headers["bob"],
        json={"version": handoff["version"], "note": "请补充接口文档"},
    )
    assert revised.status_code == 200, revised.text
    submitted = await client.post(
        f"/api/v1/work-items/{ctx.source.id}/submit", headers=ctx.headers["alice"],
        json={"version": revised.json()["source_work_item"]["version"]},
    )
    assert submitted.status_code == 409, submitted.text
    resent = await _send(client, ctx, version=revised.json()["source_work_item"]["version"])
    assert resent["status"] == "pending"


async def test_handoff_creation_replays_same_idempotency_key(client, handoff_ctx):
    ctx = handoff_ctx
    headers = {**ctx.headers["alice"], "Idempotency-Key": "handoff-create"}
    url = f"/api/v1/work-items/{ctx.source.id}/handoffs"
    first = await client.post(url, headers=headers, json=_payload(ctx))
    second = await client.post(url, headers=headers, json=_payload(ctx))
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json() == second.json()


@pytest.mark.parametrize("action", ["accept", "request-changes"])
async def test_handoff_response_replays_same_key_only_for_same_user(client, handoff_ctx, action):
    ctx = handoff_ctx
    handoff = await _send(client, ctx)
    key = "handoff-response"
    headers = {**ctx.headers["bob"], "Idempotency-Key": key}
    url = f"/api/v1/handoffs/{handoff['id']}/{action}"
    payload = {"version": handoff["version"], "note": "接收意见"}
    first = await client.post(url, headers=headers, json=payload)
    second = await client.post(url, headers=headers, json=payload)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json() == second.json()
    unauthorized = await client.post(
        url, headers={**ctx.headers["carol"], "Idempotency-Key": key}, json=payload,
    )
    assert unauthorized.status_code == 403, unauthorized.text


async def test_receiver_reads_only_handed_over_file_version(client, handoff_ctx, tmp_path):
    ctx = handoff_ctx
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    try:
        files = []
        deliveries = []
        for content in (b"private old version", b"shared interface specification"):
            uploaded = await client.post(
                "/api/v1/files", headers=ctx.headers["alice"],
                data={"work_item_id": str(ctx.source.id)},
                files={"file": ("interface.txt", content, "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            files.append(uploaded.json()["id"])
            delivered = await client.post(
                f"/api/v1/work-items/{ctx.source.id}/deliverables", headers=ctx.headers["alice"],
                json={"type": "file", "file_id": files[-1]},
            )
            assert delivered.status_code == 201, delivered.text
            deliveries.append(delivered.json()["id"])
        handoff = await _send(client, ctx, deliverable_id=deliveries[-1])
        for file_id, actor, expected in (
            (files[0], "bob", 403), (files[1], "bob", 200),
            (files[1], "carol", 403), (files[1], "leader", 200),
        ):
            downloaded = await client.get(f"/api/v1/files/{file_id}/download", headers=ctx.headers[actor])
            assert downloaded.status_code == expected, downloaded.text
        source = await client.get(f"/api/v1/work-items/{ctx.source.id}", headers=ctx.headers["bob"])
        assert source.status_code == 404
        accepted = await client.post(
            f"/api/v1/handoffs/{handoff['id']}/accept", headers=ctx.headers["bob"],
            json={"version": handoff["version"]},
        )
        assert accepted.status_code == 200, accepted.text
        target = await client.get(f"/api/v1/work-items/{ctx.target.id}", headers=ctx.headers["leader"])
        reassigned = await client.patch(
            f"/api/v1/work-items/{ctx.target.id}", headers=ctx.headers["leader"],
            json={"version": target.json()["version"], "assignee_id": str(ctx.members["carol"].id)},
        )
        assert reassigned.status_code == 200, reassigned.text
        downloaded = await client.get(f"/api/v1/files/{files[1]}/download", headers=ctx.headers["carol"])
        assert downloaded.status_code == 200, downloaded.text
    finally:
        app.dependency_overrides.pop(get_storage_provider, None)
