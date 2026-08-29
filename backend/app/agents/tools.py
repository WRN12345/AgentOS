"""Agent 工具注册表与权限护栏。

注册表只包含两类工具：
- read_query：使用 SQLAlchemy select 加载最小业务上下文；
- write_suggestion：唯一写工具，只写 agent_suggestions，不改变正式业务状态。

FORBIDDEN_OPERATIONS 中的操作不得注册为工具，只能由人通过正式业务命令执行。

护栏由 tests/test_agent_guardrails.py 强制：
- 每个工具必须显式设置 AgentTool.kind，注册表不得包含禁止操作；
- 本模块不得导入 domain service；记忆检索只能使用 memory.search 的鉴权只读路径；
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
from app.domains.dev_docs.models import DevDoc
from app.domains.files.models import StoredFile
from app.domains.identity.models import User
from app.domains.memory.member_stats import member_completion_stats
from app.domains.memory.search import CALLER_AGENT_ASSIGNMENT, search_memory
from app.domains.project.models import MemberCapability, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.state_machine import TransferStatus
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import ACTIVE_STATUSES, WorkItemStatus

#: 工具只允许只读业务查询和建议写入两类。
ToolKind = Literal["read_query", "write_suggestion"]


@dataclass(frozen=True)
class AgentTool:
    """注册表中的一个 Agent 工具。"""

    name: str
    kind: ToolKind
    func: Any  # async 可调用对象，首个参数为 session
    description: str


# 只读业务查询工具


async def get_work_item_overview(
    session: AsyncSession,
    work_item_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
) -> dict | None:
    """工作项概览：标题、状态、负责人、DDL、优先级、验收标准。

    项目归属校验：跨项目工作项视为不可见（返回 None，不泄漏其他项目上下文）。
    """
    item = await session.get(WorkItem, work_item_id)
    if item is None or item.project_id != project_id:
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
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询进行中工作项，默认返回全部活跃状态。

    project_id 用于项目隔离，不能泄漏其他项目的上下文。
    """
    stmt = select(WorkItem).order_by(WorkItem.due_at.asc().nulls_last()).limit(limit)
    if project_id is not None:
        stmt = stmt.where(WorkItem.project_id == project_id)
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


async def list_member_capabilities(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[dict]:
    """查询当前项目成员的能力标签、熟练度和负责人确认状态。"""
    rows = (
        await session.execute(
            select(ProjectMember, MemberCapability)
            .join(MemberCapability, MemberCapability.member_id == ProjectMember.id)
            .where(ProjectMember.is_active.is_(True), ProjectMember.project_id == project_id)
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


async def list_capability_tags(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[str]:
    """查询当前项目去重后的 member_capabilities.tag，约束 involved_aspects 取值。"""
    rows = (
        await session.execute(
            select(MemberCapability.tag)
            .join(ProjectMember, ProjectMember.id == MemberCapability.member_id)
            .where(ProjectMember.project_id == project_id)
            .distinct()
            .order_by(MemberCapability.tag)
        )
    ).all()
    return [tag for (tag,) in rows]


async def list_assignable_members(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[dict]:
    """查询当前项目可分配成员的 display_name 和 username。

    停用成员不可分配，因此不进入清单。
    """
    rows = (
        await session.execute(
            select(ProjectMember.id, ProjectMember.display_name, User.username)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.is_active.is_(True), ProjectMember.project_id == project_id)
            .order_by(ProjectMember.display_name)
        )
    ).all()
    return [
        {"member_id": str(member_id), "display_name": display_name, "username": username}
        for member_id, display_name, username in rows
    ]


async def get_member_workload(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[dict]:
    """统计当前项目各活跃成员名下的活跃工作项数。

    成员和工作项均按 project_id 过滤，避免跨项目污染负载统计。
    """
    rows = (
        await session.execute(
            select(ProjectMember.id, ProjectMember.display_name, func.count(WorkItem.id))
            .outerjoin(
                WorkItem,
                (WorkItem.assignee_id == ProjectMember.id)
                & (WorkItem.project_id == project_id)
                & (WorkItem.status.in_(ACTIVE_STATUSES)),
            )
            .where(ProjectMember.is_active.is_(True), ProjectMember.project_id == project_id)
            .group_by(ProjectMember.id, ProjectMember.display_name)
            .order_by(ProjectMember.display_name)
        )
    ).all()
    return [
        {"member_id": str(member_id), "display_name": name, "active_work_items": count}
        for member_id, name, count in rows
    ]


async def list_deliverable_metadata(
    session: AsyncSession,
    work_item_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
) -> list[dict]:
    """查询交付物初审所需的最小上下文。

    text 和 git_link 返回 content；file 只返回文件名、大小、MIME 和 sha256 等元数据，
    绝不读取文件原文。查询按 project_id 隔离。
    """
    rows = (
        await session.execute(
            select(Deliverable, StoredFile)
            .outerjoin(StoredFile, Deliverable.stored_file_id == StoredFile.id)
            .where(
                Deliverable.work_item_id == work_item_id,
                Deliverable.project_id == project_id,
            )
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
            # 文件类交付物不得读取原文，避免向 Agent 暴露非必要内容。
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


# 风险扫描只读工具


async def get_dev_doc(
    session: AsyncSession,
    work_item_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None = None,
) -> dict | None:
    """查询工作项的开发文档状态、版本和 Markdown 正文。

    项目归属通过 work_items 校验，跨项目文档视为不存在。
    """
    doc = (
        await session.execute(
            select(DevDoc)
            .join(WorkItem, WorkItem.id == DevDoc.work_item_id)
            .where(DevDoc.work_item_id == work_item_id, WorkItem.project_id == project_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        return None
    return {
        "id": str(doc.id),
        "work_item_id": str(doc.work_item_id),
        "status": doc.status,
        "doc_version": doc.doc_version,
        "content": doc.content,
    }


async def list_blocked_items(
    session: AsyncSession, *, project_id: uuid.UUID | None = None, limit: int = 50
) -> list[dict]:
    """查询当前项目阻塞中的工作项及阻塞起始时间。"""
    items = (
        (
            await session.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == WorkItemStatus.BLOCKED.value,
                    WorkItem.project_id == project_id,
                )
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
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    days: int = 30,
    limit: int = 100,
) -> list[dict]:
    """查询近期转派申请，默认覆盖近 30 天。

    transfer_requests 没有 project_id，通过关联工作项执行项目隔离。
    """
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(TransferRequest)
                .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
                .where(TransferRequest.created_at >= since, WorkItem.project_id == project_id)
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


async def list_waiting_collaborations(
    session: AsyncSession, *, project_id: uuid.UUID | None = None, limit: int = 50
) -> list[dict]:
    """查询未终态的协作请求，用于识别协作等待风险。

    collaboration_requests 没有 project_id，通过关联工作项执行项目隔离。
    """
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
                .join(WorkItem, WorkItem.id == CollaborationRequest.work_item_id)
                .where(
                    CollaborationRequest.status.in_(waiting),
                    WorkItem.project_id == project_id,
                )
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


# 摘要统计只读工具


async def get_work_item_status_counts(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> dict[str, int]:
    """统计当前项目各状态的工作项数量。"""
    rows = (
        await session.execute(
            select(WorkItem.status, func.count())
            .where(WorkItem.project_id == project_id)
            .group_by(WorkItem.status)
        )
    ).all()
    return {status: count for status, count in rows}


async def list_recently_completed_work_items(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    days: int = 7,
    limit: int = 20,
) -> list[dict]:
    """按完成时间倒序查询当前项目近期完成的工作项，默认覆盖近 7 天。"""
    since = datetime.now(UTC) - timedelta(days=days)
    items = (
        (
            await session.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == WorkItemStatus.COMPLETED.value,
                    WorkItem.updated_at >= since,
                    WorkItem.project_id == project_id,
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


async def list_pending_approvals(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> dict[str, list[dict]]:
    """汇总待审工作项、待批转派和待批 DDL 变更。

    transfers 和 deadline_changes 没有 project_id，通过关联工作项执行项目隔离。
    """
    in_review = (
        (
            await session.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == WorkItemStatus.IN_REVIEW.value,
                    WorkItem.project_id == project_id,
                )
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
                .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
                .where(
                    TransferRequest.status == TransferStatus.PENDING.value,
                    WorkItem.project_id == project_id,
                )
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
                .join(WorkItem, WorkItem.id == DeadlineChangeRequest.work_item_id)
                .where(
                    DeadlineChangeRequest.status.in_(sorted(DEADLINE_PENDING_STATUSES)),
                    WorkItem.project_id == project_id,
                )
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


# 记忆检索只读工具


async def search_project_documents(
    session: AsyncSession,
    query: str,
    *,
    project_id: uuid.UUID,
    limit: int = 5,
) -> list[dict]:
    """通过向量语义检索项目文档片段，供拆解和分配参考。

    使用 caller=agent_assignment 的鉴权路径，检索层强制只命中当前项目的最新文档块。
    """
    results = await search_memory(
        session,
        member=None,
        is_admin=False,
        project_id=project_id,
        query=query,
        caller=CALLER_AGENT_ASSIGNMENT,
        source_types=["document"],
        limit=limit,
    )
    return [
        {"content": r.content, "source_id": str(r.source_id), "distance": r.distance}
        for r in results
    ]


async def search_history_records(
    session: AsyncSession,
    query: str,
    *,
    project_id: uuid.UUID,
    limit: int = 5,
) -> list[dict]:
    """检索历史拆解、分配记录和已完成工作项结论，供新需求参考。"""
    results = await search_memory(
        session,
        member=None,
        is_admin=False,
        project_id=project_id,
        query=query,
        caller=CALLER_AGENT_ASSIGNMENT,
        source_types=["history"],
        limit=limit,
    )
    return [
        {"content": r.content, "source_id": str(r.source_id), "distance": r.distance}
        for r in results
    ]


async def get_member_stats(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[dict]:
    """查询成员完成数、按时率、负载和样本量。

    is_active=False 的成员保留历史统计，但不进入分配候选。
    """
    stats = await member_completion_stats(session, project_id=project_id)
    return [
        {
            "member_id": str(s.member_id),
            "display_name": s.display_name,
            "is_active": s.is_active,
            "completed_total": s.completed_total,
            "active_now": s.active_now,
            "completed_recent": s.completed_recent,
            "on_time_rate": s.on_time_rate,
            "sample_sufficient": s.sample_sufficient,
        }
        for s in stats
    ]


async def search_member_profiles(
    session: AsyncSession,
    query: str,
    *,
    project_id: uuid.UUID,
    limit: int = 5,
) -> list[dict]:
    """检索统计数据无法表达的成员特质和历史背景。

    agent_assignment 场景允许命中随成员保留的跨项目档案，供分配决策参考。
    """
    results = await search_memory(
        session,
        member=None,
        is_admin=False,
        project_id=project_id,
        query=query,
        caller=CALLER_AGENT_ASSIGNMENT,
        source_types=["profile"],
        limit=limit,
    )
    return [
        {"content": r.content, "source_id": str(r.source_id), "distance": r.distance}
        for r in results
    ]


# 建议写入工具


async def write_suggestion(
    session: AsyncSession, *, envelope: AgentSuggestionEnvelope
) -> AgentSuggestion:
    """写入一条 AgentSuggestion，仅 flush，由调用方与通知一起 commit。

    这是 Agent 唯一的写路径，不得修改任何正式业务状态。
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


#: 工具注册表。新增工具必须显式设置 kind 并通过护栏测试。
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
            name="list_capability_tags",
            kind="read_query",
            func=list_capability_tags,
            description="查询成员技能标签去重词表（约束需求流水线的 involved_aspects 取值）",
        ),
        AgentTool(
            name="list_assignable_members",
            kind="read_query",
            func=list_assignable_members,
            description="查询可分配成员清单（display_name/username，排除管理员与停用成员）",
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
            name="get_dev_doc",
            kind="read_query",
            func=get_dev_doc,
            description="查询工作项的开发文档（状态/提交次数/Markdown 正文，供文档初审）",
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
            name="search_project_documents",
            kind="read_query",
            func=search_project_documents,
            description="检索项目文档片段（语义检索，拆解/分配时参考项目资料）",
        ),
        AgentTool(
            name="search_history_records",
            kind="read_query",
            func=search_history_records,
            description="检索历史拆解/分配记录与已完成工作项结论（参考以往经验）",
        ),
        AgentTool(
            name="get_member_stats",
            kind="read_query",
            func=get_member_stats,
            description="查询成员完成统计：完成数/按时率/负载/样本量（分配参考事实记录）",
        ),
        AgentTool(
            name="search_member_profiles",
            kind="read_query",
            func=search_member_profiles,
            description="检索成员文字档案（成员特质与历史背景，分配参考）",
        ),
        AgentTool(
            name="write_suggestion",
            kind="write_suggestion",
            func=write_suggestion,
            description="写入一条 Agent 建议（agent_suggestions 表，唯一写工具）",
        ),
    )
}

#: 禁止注册为 Agent 工具的操作，只能由人通过 API 到 domain service 的正式命令完成。
FORBIDDEN_OPERATIONS: list[dict[str, str]] = [
    {"operation": "create_work_item", "description": "创建正式工作项"},
    {"operation": "change_assignee", "description": "修改负责人"},
    {"operation": "approve_transfer", "description": "审批转派"},
    {"operation": "change_deadline", "description": "修改 DDL"},
    {"operation": "approve_review", "description": "通过审核"},
    {"operation": "delete_file_or_record", "description": "删除文件或业务记录"},
    {"operation": "merge_code", "description": "合并代码"},
]
