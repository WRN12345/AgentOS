"""Agent 工具注册表与权限护栏（10.3 节，T5.3）。

注册表只包含两类工具：
- read_query：只读业务查询（纯 SQLAlchemy select，供 T5.4/T5.5 的 Agent
  加载最小上下文——工作项、成员能力、负载、交付物元数据等）；
- write_suggestion：写入建议（唯一写工具，只写 agent_suggestions 表；
  Agent 只生成建议，不能改变正式业务状态）。

10.3 节禁止的操作一律不注册为工具（见 FORBIDDEN_OPERATIONS 结构化清单）：
创建正式工作项、修改负责人、审批转派、修改 DDL、通过审核、
删除文件或业务记录、合并代码。

护栏约定（tests/test_agent_guardrails.py 自动化强制，第 22 章标准 10）：
- 每个工具在 AgentTool.kind 上显式标记类别，注册表与 FORBIDDEN 不相交；
- 本模块只 import 各 domain 的 models / 只读常量（state_machine），
  不得 import 任何 domain 的 service（写命令都定义在 service 层）；
- read_query 工具实现不得出现 session.add / session.delete 等写调用。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentSuggestion
from app.agents.schemas.suggestion import AgentSuggestionEnvelope
from app.domains.collaboration.models import CollaborationRequest
from app.domains.collaboration.state_machine import CollaborationStatus
from app.domains.deadlines.models import DeadlineChangeRequest
from app.domains.deadlines.state_machine import PENDING_STATUSES as DEADLINE_PENDING_STATUSES
from app.domains.deliverables.models import Deliverable
from app.domains.files.models import StoredFile
from app.domains.project.models import MemberCapability, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.state_machine import TransferStatus
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import ACTIVE_STATUSES, WorkItemStatus

#: 工具类别：只读业务查询 / 写入建议（仅此两类，10.3 节）
ToolKind = Literal["read_query", "write_suggestion"]


@dataclass(frozen=True)
class AgentTool:
    """注册表中的一个 Agent 工具。"""

    name: str
    kind: ToolKind
    func: Any  # async (session, **kwargs) -> ...
    description: str


# ---------- 只读业务查询工具（read_query） ----------


async def get_work_item_overview(session: AsyncSession, work_item_id: uuid.UUID) -> dict | None:
    """工作项概览：标题、状态、负责人、DDL、优先级、验收标准。"""
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        return None
    return {
        "id": str(item.id),
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_id": str(item.assignee_id),
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "acceptance_criteria": item.acceptance_criteria,
    }


async def list_open_work_items(
    session: AsyncSession, *, status: str | None = None, limit: int = 50
) -> list[dict]:
    """进行中工作项清单（默认全部活跃状态，供风险/规划类 Agent 扫描）。"""
    stmt = select(WorkItem).order_by(WorkItem.due_at.asc().nulls_last()).limit(limit)
    if status is not None:
        stmt = stmt.where(WorkItem.status == status)
    else:
        stmt = stmt.where(WorkItem.status.in_(ACTIVE_STATUSES))
    items = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "status": item.status,
            "priority": item.priority,
            "assignee_id": str(item.assignee_id),
            "due_at": item.due_at.isoformat() if item.due_at else None,
        }
        for item in items
    ]


async def list_member_capabilities(session: AsyncSession) -> list[dict]:
    """成员能力清单：标签、熟练度、负责人确认状态（6.2 节，供分配建议引用）。"""
    rows = (
        await session.execute(
            select(ProjectMember, MemberCapability)
            .join(MemberCapability, MemberCapability.member_id == ProjectMember.id)
            .where(ProjectMember.is_active.is_(True))
            .order_by(ProjectMember.display_name, MemberCapability.tag)
        )
    ).all()
    return [
        {
            "member_id": str(member.id),
            "display_name": member.display_name,
            "tag": capability.tag,
            "proficiency": capability.proficiency,
            "confirmed": capability.confirmed,
        }
        for member, capability in rows
    ]


async def get_member_workload(session: AsyncSession) -> list[dict]:
    """成员当前负载：各活跃成员名下的活跃工作项数（6.2 节，供分配建议引用）。"""
    rows = (
        await session.execute(
            select(ProjectMember.id, ProjectMember.display_name, func.count(WorkItem.id))
            .outerjoin(
                WorkItem,
                (WorkItem.assignee_id == ProjectMember.id)
                & (WorkItem.status.in_(ACTIVE_STATUSES)),
            )
            .where(ProjectMember.is_active.is_(True))
            .group_by(ProjectMember.id, ProjectMember.display_name)
            .order_by(ProjectMember.display_name)
        )
    ).all()
    return [
        {"member_id": str(member_id), "display_name": name, "active_work_items": count}
        for member_id, name, count in rows
    ]


async def list_deliverable_metadata(
    session: AsyncSession, work_item_id: uuid.UUID
) -> list[dict]:
    """交付物最小上下文（16 节）：文本正文 / Git 链接文本 / 文件元数据。

    text / git_link 类型的 content（正文、链接文本）属于初审最小上下文；
    file 类型只返回文件名 / 大小 / MIME / sha256 等元数据，绝不读取文件原文。
    """
    rows = (
        await session.execute(
            select(Deliverable, StoredFile)
            .outerjoin(StoredFile, Deliverable.stored_file_id == StoredFile.id)
            .where(Deliverable.work_item_id == work_item_id)
            .order_by(Deliverable.version)
        )
    ).all()
    return [
        {
            "id": str(d.id),
            "type": d.type,
            "version": d.version,
            "submitted_by": str(d.submitted_by),
            "stored_file_id": str(d.stored_file_id) if d.stored_file_id else None,
            # 仅文本/Git 链接交付物带正文；文件类一律 None（不读原文）
            "content": d.content if d.type in ("text", "git_link") else None,
            "file": (
                {
                    "original_filename": f.original_filename,
                    "size_bytes": f.size_bytes,
                    "mime_type": f.mime_type,
                    "sha256": f.sha256,
                }
                if f is not None
                else None
            ),
            "created_at": d.created_at.isoformat(),
        }
        for d, f in rows
    ]


# ---------- 风险扫描只读工具（T5.5 Workflow Risk Agent） ----------


async def list_blocked_items(session: AsyncSession, *, limit: int = 50) -> list[dict]:
    """阻塞中的工作项清单（含阻塞起始时间，供风险 Agent 评估阻塞时长）。"""
    items = (
        (
            await session.execute(
                select(WorkItem)
                .where(WorkItem.status == WorkItemStatus.BLOCKED.value)
                .order_by(WorkItem.updated_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "status": item.status,
            "priority": item.priority,
            "assignee_id": str(item.assignee_id),
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "blocked_since": item.updated_at.isoformat(),
        }
        for item in items
    ]


async def list_transfer_history(
    session: AsyncSession, *, days: int = 30, limit: int = 100
) -> list[dict]:
    """近期转派申请记录（默认近 30 天，供风险 Agent 识别频繁转派）。"""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(TransferRequest)
                .where(TransferRequest.created_at >= since)
                .order_by(TransferRequest.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(t.id),
            "work_item_id": str(t.work_item_id),
            "from_member_id": str(t.from_member_id),
            "to_member_id": str(t.to_member_id),
            "status": t.status,
            "created_at": t.created_at.isoformat(),
        }
        for t in rows
    ]


async def list_waiting_collaborations(session: AsyncSession, *, limit: int = 50) -> list[dict]:
    """等待中的协作请求（未终态：已请求/已接受/处理中/被要求修改），供识别协作等待风险。"""
    waiting = (
        CollaborationStatus.REQUESTED.value,
        CollaborationStatus.ACCEPTED.value,
        CollaborationStatus.IN_PROGRESS.value,
        CollaborationStatus.REVISION_REQUESTED.value,
    )
    rows = (
        (
            await session.execute(
                select(CollaborationRequest)
                .where(CollaborationRequest.status.in_(waiting))
                .order_by(CollaborationRequest.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(c.id),
            "work_item_id": str(c.work_item_id),
            "title": c.title,
            "status": c.status,
            "requester_id": str(c.requester_id),
            "assignee_id": str(c.assignee_id),
            "due_at": c.due_at.isoformat() if c.due_at else None,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


# ---------- 摘要统计只读工具（T5.5 Summary Agent） ----------


async def get_work_item_status_counts(session: AsyncSession) -> dict[str, int]:
    """各状态工作项数量（项目进展统计，供摘要 Agent 引用真实数据）。"""
    rows = (
        await session.execute(select(WorkItem.status, func.count()).group_by(WorkItem.status))
    ).all()
    return {status: count for status, count in rows}


async def list_recently_completed_work_items(
    session: AsyncSession, *, days: int = 7, limit: int = 20
) -> list[dict]:
    """近期完成的工作项（默认近 7 天，按完成时间倒序，供摘要 Agent 使用）。"""
    since = datetime.now(UTC) - timedelta(days=days)
    items = (
        (
            await session.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == WorkItemStatus.COMPLETED.value,
                    WorkItem.updated_at >= since,
                )
                .order_by(WorkItem.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "assignee_id": str(item.assignee_id),
            "completed_at": item.updated_at.isoformat(),
        }
        for item in items
    ]


async def list_pending_approvals(session: AsyncSession) -> dict[str, list[dict]]:
    """待负责人审批事项汇总：待审工作项（IN_REVIEW）、待批转派、待批 DDL 变更。"""
    in_review = (
        (
            await session.execute(
                select(WorkItem)
                .where(WorkItem.status == WorkItemStatus.IN_REVIEW.value)
                .order_by(WorkItem.updated_at)
            )
        )
        .scalars()
        .all()
    )
    transfers = (
        (
            await session.execute(
                select(TransferRequest)
                .where(TransferRequest.status == TransferStatus.PENDING.value)
                .order_by(TransferRequest.created_at)
            )
        )
        .scalars()
        .all()
    )
    deadline_changes = (
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .where(DeadlineChangeRequest.status.in_(sorted(DEADLINE_PENDING_STATUSES)))
                .order_by(DeadlineChangeRequest.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "in_review_work_items": [
            {
                "id": str(item.id),
                "title": item.title,
                "assignee_id": str(item.assignee_id),
                "submitted_at": item.updated_at.isoformat(),
            }
            for item in in_review
        ],
        "pending_transfers": [
            {
                "id": str(t.id),
                "work_item_id": str(t.work_item_id),
                "from_member_id": str(t.from_member_id),
                "to_member_id": str(t.to_member_id),
                "created_at": t.created_at.isoformat(),
            }
            for t in transfers
        ],
        "pending_deadline_changes": [
            {
                "id": str(d.id),
                "work_item_id": str(d.work_item_id),
                "status": d.status,
                "new_due_at": d.new_due_at.isoformat(),
                "created_at": d.created_at.isoformat(),
            }
            for d in deadline_changes
        ],
    }


# ---------- 写入建议工具（write_suggestion，唯一写工具） ----------


async def write_suggestion(
    session: AsyncSession, *, envelope: AgentSuggestionEnvelope
) -> AgentSuggestion:
    """写入一条 AgentSuggestion（只 flush，由调用方与通知一起统一 commit）。

    这是 Agent 唯一的写路径：只写 agent_suggestions 表，不触碰任何正式
    业务状态（10.3 节、原则 2）。
    """
    record = AgentSuggestion(
        run_id=envelope.run_id,
        suggestion_type=envelope.suggestion_type,
        content=envelope.content.model_dump(mode="json"),
        confidence=envelope.confidence,
        risks=envelope.risks,
        fact_refs=envelope.fact_refs,
        prompt_version=envelope.prompt_version,
    )
    session.add(record)
    await session.flush()
    return record


# ---------- 工具注册表 ----------

#: 工具名 → 工具。新增工具必须显式标记 kind，并通过护栏测试。
TOOL_REGISTRY: dict[str, AgentTool] = {
    tool.name: tool
    for tool in (
        AgentTool(
            name="get_work_item_overview",
            kind="read_query",
            func=get_work_item_overview,
            description="查询单个工作项的概览（标题/状态/负责人/DDL/验收标准）",
        ),
        AgentTool(
            name="list_open_work_items",
            kind="read_query",
            func=list_open_work_items,
            description="查询进行中工作项清单（可按状态过滤）",
        ),
        AgentTool(
            name="list_member_capabilities",
            kind="read_query",
            func=list_member_capabilities,
            description="查询成员能力标签、熟练度与确认状态",
        ),
        AgentTool(
            name="get_member_workload",
            kind="read_query",
            func=get_member_workload,
            description="查询各成员当前活跃工作项负载",
        ),
        AgentTool(
            name="list_deliverable_metadata",
            kind="read_query",
            func=list_deliverable_metadata,
            description="查询工作项交付物最小上下文（文本正文/Git 链接/文件元数据，不读文件原文）",
        ),
        AgentTool(
            name="list_blocked_items",
            kind="read_query",
            func=list_blocked_items,
            description="查询阻塞中的工作项清单（供风险扫描）",
        ),
        AgentTool(
            name="list_transfer_history",
            kind="read_query",
            func=list_transfer_history,
            description="查询近期转派申请记录（供识别频繁转派风险）",
        ),
        AgentTool(
            name="list_waiting_collaborations",
            kind="read_query",
            func=list_waiting_collaborations,
            description="查询等待中的协作请求（供识别协作等待风险）",
        ),
        AgentTool(
            name="get_work_item_status_counts",
            kind="read_query",
            func=get_work_item_status_counts,
            description="统计各状态工作项数量（供摘要 Agent 引用真实进展数据）",
        ),
        AgentTool(
            name="list_recently_completed_work_items",
            kind="read_query",
            func=list_recently_completed_work_items,
            description="查询近期完成的工作项（供摘要 Agent 使用）",
        ),
        AgentTool(
            name="list_pending_approvals",
            kind="read_query",
            func=list_pending_approvals,
            description="汇总待审批事项：待审工作项 / 待批转派 / 待批 DDL 变更",
        ),
        AgentTool(
            name="write_suggestion",
            kind="write_suggestion",
            func=write_suggestion,
            description="写入一条 Agent 建议（agent_suggestions 表，唯一写工具）",
        ),
    )
}

#: 10.3 节禁止注册为 Agent 工具的操作（结构化清单，护栏测试断言不相交）。
#: 这些操作只能由人通过正式业务命令（API → domain service）完成。
FORBIDDEN_OPERATIONS: list[dict[str, str]] = [
    {"operation": "create_work_item", "description": "创建正式工作项"},
    {"operation": "change_assignee", "description": "修改负责人"},
    {"operation": "approve_transfer", "description": "审批转派"},
    {"operation": "change_deadline", "description": "修改 DDL"},
    {"operation": "approve_review", "description": "通过审核"},
    {"operation": "delete_file_or_record", "description": "删除文件或业务记录"},
    {"operation": "merge_code", "description": "合并代码"},
]
