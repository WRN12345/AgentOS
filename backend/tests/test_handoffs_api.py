"""交接授权、乐观锁与接收方驱动的完成流程。"""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.errors import ApiException
from app.domains.audit.models import AuditEvent
from app.domains.deliverables.models import Deliverable
from app.domains.handoffs.models import DeliverableHandoff
from app.domains.handoffs.policy import ensure_no_pending_handoff
from app.domains.notifications.models import Notification
from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers


@pytest.fixture
async def context(client, project_a, leader, monkeypatch):
    for name in (
        "_dispatch_deliverable_review",
        "enqueue_work_item_conclusion_index",
        "enqueue_work_item_summary",
    ):
        monkeypatch.setattr(f"app.domains.handoffs.service.{name}", AsyncMock())
    members = {"leader": leader}
    headers = {}
    for name in ("sender", "recipient", "outsider"):
        _, members[name] = await add_member(project_a, name, "Password123!")
    for name in members:
        headers[name] = await auth_headers(
            client,
            name,
            "Leader123!" if name == "leader" else "Password123!",
            project_id=str(project_a.id),
        )
    async with async_session_factory() as session:
        source = WorkItem(
            project_id=project_a.id,
            title="Interface",
            status="IN_PROGRESS",
            assignee_id=members["sender"].id,
        )
        target = WorkItem(
            project_id=project_a.id,
            title="Consumer",
            status="READY",
            assignee_id=members["recipient"].id,
        )
        session.add_all([source, target])
        await session.flush()
        delivery = Deliverable(
            project_id=project_a.id,
            work_item_id=source.id,
            type="text",
            content="Interface v1",
            version=1,
            submitted_by=members["sender"].id,
        )
        session.add(delivery)
        await session.commit()
    return {
        "source": source,
        "target": target,
        "delivery": delivery,
        "members": members,
        "headers": headers,
        "payload": {
            "version": 1,
            "target_work_item_id": str(target.id),
            "target_version": 1,
            "deliverable_id": str(delivery.id),
        },
    }


async def create(client, context, **overrides):
    return await client.post(
        f"/api/v1/work-items/{context['source'].id}/handoffs",
        headers=context["headers"]["sender"],
        json=context["payload"] | overrides,
    )


async def decision(
    client, context, handoff, command="accept", actor="recipient", **overrides
):
    return await client.post(
        f"/api/v1/handoffs/{handoff['id']}/{command}",
        headers=context["headers"][actor],
        json={"version": handoff["version"]} | overrides,
    )


async def test_accept_completes_only_source_and_records_events(client, context):
    created = await create(client, context, note="API contract")
    assert created.status_code == 201, created.text
    handoff = created.json()
    assert handoff["source_work_item"]["status"] == "WAITING_ACCEPTANCE"
    assert handoff["source_work_item"]["version"] == 2
    assert handoff["deliverable"]["id"] == str(context["delivery"].id)
    assert handoff["recipient"]["id"] == str(context["members"]["recipient"].id)
    accepted = await decision(client, context, handoff, note="Usable")
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["status"] == "accepted"
    assert result["source_work_item"]["status"] == "COMPLETED"
    assert result["source_work_item"]["version"] == 3
    assert result["target_work_item"]["status"] == "READY"
    assert result["target_work_item"]["version"] == 1
    assert result["response_note"] == "Usable"
    assert result["responded_at"]
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WorkItem)) == 2
        actions = (await session.scalars(select(AuditEvent.action))).all()
        assert "handoff.created" in actions and "handoff.accepted" in actions
        notices = (
            await session.scalars(
                select(Notification).where(Notification.type.like("handoff.%"))
            )
        ).all()
        assert len(notices) == 4
        assert {n.recipient_id for n in notices} == {
            context["members"][name].id for name in ("sender", "recipient")
        }
        await ensure_no_pending_handoff(session, context["target"].id)


async def test_duplicate_and_stale_responses_conflict(client, context):
    handoff = (await create(client, context)).json()
    assert (await create(client, context)).status_code == 409
    assert (await decision(client, context, handoff, version=2)).status_code == 409
    assert (await decision(client, context, handoff)).status_code == 200
    assert (await decision(client, context, handoff)).status_code == 409
    assert (await decision(client, context, handoff, version=2)).status_code == 409


async def test_rejection_requires_note_and_allows_resubmission(client, context):
    handoff = (await create(client, context)).json()
    for note in (None, "", "  "):
        assert (
            await decision(client, context, handoff, "request-changes", note=note)
        ).status_code == 422
    rejected = await decision(
        client, context, handoff, "request-changes", note="Document errors"
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["source_work_item"]["status"] == "IN_PROGRESS"
    assert rejected.json()["status"] == "changes_requested"
    assert rejected.json()["target_work_item"]["status"] == "READY"
    resubmitted = await create(client, context, version=3)
    assert resubmitted.status_code == 201, resubmitted.text
    assert resubmitted.json()["id"] != handoff["id"]


@pytest.mark.parametrize("actor", ["leader", "outsider", "recipient"])
async def test_only_source_owner_can_create(client, context, actor):
    response = await client.post(
        f"/api/v1/work-items/{context['source'].id}/handoffs",
        headers=context["headers"][actor],
        json=context["payload"],
    )
    assert response.status_code == 403


@pytest.mark.parametrize("actor", ["leader", "outsider", "sender"])
@pytest.mark.parametrize("command", ["accept", "request-changes"])
async def test_only_recipient_can_respond_without_leader_override(
    client, context, actor, command
):
    handoff = (await create(client, context)).json()
    assert (
        await decision(client, context, handoff, command, actor, note="Review")
    ).status_code == 403


@pytest.mark.parametrize("field", ["version", "target_version"])
async def test_create_checks_both_versions(client, context, field):
    assert (await create(client, context, **{field: 2})).status_code == 409
    async with async_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(DeliverableHandoff))
            == 0
        )


@pytest.mark.parametrize(
    "which,status",
    [
        ("source", "READY"),
        ("source", "IN_REVIEW"),
        ("target", "IN_PROGRESS"),
        ("target", "COMPLETED"),
    ],
)
async def test_create_requires_source_in_progress_and_target_ready(
    client, context, which, status
):
    async with async_session_factory() as session:
        item = await session.get(WorkItem, context[which].id)
        item.status = status
        await session.commit()
    assert (await create(client, context)).status_code == 409


async def test_exact_latest_source_deliverable_required(client, context):
    async with async_session_factory() as session:
        newer = Deliverable(
            project_id=context["source"].project_id,
            work_item_id=context["source"].id,
            type="text",
            content="v2",
            version=2,
            submitted_by=context["members"]["sender"].id,
        )
        other = Deliverable(
            project_id=context["source"].project_id,
            work_item_id=context["target"].id,
            type="text",
            content="unrelated",
            version=1,
            submitted_by=context["members"]["recipient"].id,
        )
        session.add_all([newer, other])
        await session.commit()
    for delivery_id in (context["delivery"].id, other.id, uuid.uuid4()):
        assert (
            await create(client, context, deliverable_id=str(delivery_id))
        ).status_code == 409
    assert (
        await create(client, context, deliverable_id=str(newer.id))
    ).status_code == 201


async def test_target_must_differ_and_have_other_active_assignee(client, context):
    assert (
        await create(client, context, target_work_item_id=str(context["source"].id))
    ).status_code == 422
    async with async_session_factory() as session:
        target = await session.get(WorkItem, context["target"].id)
        target.assignee_id = context["members"]["sender"].id
        await session.commit()
    assert (await create(client, context)).status_code == 422
    async with async_session_factory() as session:
        target = await session.get(WorkItem, context["target"].id)
        target.assignee_id = context["members"]["recipient"].id
        member = await session.get(ProjectMember, target.assignee_id)
        member.is_active = False
        await session.commit()
    assert (await create(client, context)).status_code == 422


async def test_recipient_cannot_be_supplied_by_client(client, context):
    assert (
        await create(
            client, context, recipient_id=str(context["members"]["outsider"].id)
        )
    ).status_code == 422


async def test_cross_project_items_and_handoffs_are_hidden(client, context, project_b):
    _, foreign = await add_member(project_b, "foreign", "Password123!")
    foreign_headers = await auth_headers(
        client, "foreign", "Password123!", project_id=str(project_b.id)
    )
    async with async_session_factory() as session:
        target = WorkItem(
            project_id=project_b.id,
            title="Secret",
            status="READY",
            assignee_id=foreign.id,
        )
        session.add(target)
        await session.commit()
    assert (
        await create(client, context, target_work_item_id=str(target.id))
    ).status_code == 404
    handoff = (await create(client, context)).json()
    assert (
        await client.get(
            f"/api/v1/work-items/{context['source'].id}/handoffs",
            headers=foreign_headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/handoffs/{handoff['id']}/accept",
            headers=foreign_headers,
            json={"version": 1},
        )
    ).status_code == 404
    assert (
        await client.get("/api/v1/handoffs?role=received", headers=foreign_headers)
    ).json() == []


async def test_targets_are_minimal_and_owner_or_leader_only(client, context):
    path = f"/api/v1/handoff-targets?source_work_item_id={context['source'].id}"
    for actor in ("sender", "leader"):
        response = await client.get(path, headers=context["headers"][actor])
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert set(response.json()[0]) == {
            "id",
            "title",
            "status",
            "version",
            "assignee",
        }
        assert set(response.json()[0]["assignee"]) == {"id", "display_name"}
    assert (
        await client.get(path, headers=context["headers"]["outsider"])
    ).status_code == 403


async def test_visibility_does_not_expand_source_access(client, context):
    handoff = (await create(client, context)).json()
    path = f"/api/v1/work-items/{context['source'].id}/handoffs"
    for actor in ("sender", "recipient", "leader"):
        assert (await client.get(path, headers=context["headers"][actor])).json()[0][
            "id"
        ] == handoff["id"]
    assert (
        await client.get(path, headers=context["headers"]["outsider"])
    ).status_code == 403
    assert (
        await client.get(
            f"/api/v1/work-items/{context['source'].id}",
            headers=context["headers"]["recipient"],
        )
    ).status_code in (403, 404)
    async with async_session_factory() as session:
        session.add(
            WorkItemCollaborator(
                work_item_id=context["target"].id,
                member_id=context["members"]["outsider"].id,
            )
        )
        await session.commit()
    target_path = f"/api/v1/work-items/{context['target'].id}/handoffs"
    assert (
        await client.get(target_path, headers=context["headers"]["outsider"])
    ).json() == []
    assert (await decision(client, context, handoff)).status_code == 200
    assert (
        await client.get(target_path, headers=context["headers"]["outsider"])
    ).json()[0]["id"] == handoff["id"]
    assert (
        await client.get(
            "/api/v1/handoffs?role=received&status=pending",
            headers=context["headers"]["recipient"],
        )
    ).json() == []


async def test_response_rechecks_target_assignee(client, context):
    handoff = (await create(client, context)).json()
    async with async_session_factory() as session:
        target = await session.get(WorkItem, context["target"].id)
        target.assignee_id = context["members"]["outsider"].id
        await session.commit()
    assert (await decision(client, context, handoff)).status_code == 403
    assert (
        await decision(client, context, handoff, actor="outsider")
    ).status_code == 403


async def test_policy_blocks_both_endpoints(client, context):
    await create(client, context)
    async with async_session_factory() as session:
        for which in ("source", "target"):
            await session.get(WorkItem, context[which].id, with_for_update=True)
            with pytest.raises(ApiException) as error:
                await ensure_no_pending_handoff(session, context[which].id)
            assert error.value.status_code == 409


async def test_concurrent_creates_and_responses_have_one_winner(client, context):
    results = await asyncio.gather(create(client, context), create(client, context))
    assert sorted(r.status_code for r in results) == [201, 409]
    handoff = next(r.json() for r in results if r.status_code == 201)
    results = await asyncio.gather(
        decision(client, context, handoff),
        decision(client, context, handoff, "request-changes", note="More detail"),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_background_failures_do_not_rollback_handoff(
    client, context, monkeypatch
):
    for name in (
        "_dispatch_deliverable_review",
        "enqueue_work_item_conclusion_index",
        "enqueue_work_item_summary",
    ):
        monkeypatch.setattr(
            f"app.domains.handoffs.service.{name}",
            AsyncMock(side_effect=RuntimeError("offline")),
        )
    response = await create(client, context)
    assert response.status_code == 201
    response = await decision(client, context, response.json())
    assert response.status_code == 200
    assert response.json()["source_work_item"]["status"] == "COMPLETED"


async def test_inbox_role_filters_leaders_and_members(client, context):
    handoff = (await create(client, context)).json()
    for actor, role, expected in (
        ("recipient", "received", 1),
        ("recipient", "sent", 0),
        ("sender", "sent", 1),
        ("sender", "received", 0),
        ("outsider", "received", 0),
        ("leader", "received", 0),
        ("leader", "sent", 0),
    ):
        response = await client.get(
            f"/api/v1/handoffs?role={role}&status=pending",
            headers=context["headers"][actor],
        )
        assert response.status_code == 200
        assert len(response.json()) == expected
    leader_view = await client.get(
        f"/api/v1/work-items/{context['source'].id}/handoffs",
        headers=context["headers"]["leader"],
    )
    assert leader_view.json()[0]["id"] == handoff["id"]
    async with async_session_factory() as session:
        own_handoff = await session.get(DeliverableHandoff, uuid.UUID(handoff["id"]))
        own_handoff.recipient_id = context["members"]["leader"].id
        await session.commit()
    received = await client.get(
        "/api/v1/handoffs?role=received&status=pending",
        headers=context["headers"]["leader"],
    )
    assert [row["id"] for row in received.json()] == [handoff["id"]]
    sent = await client.get(
        "/api/v1/handoffs?role=sent",
        headers=context["headers"]["leader"],
    )
    assert sent.json() == []
