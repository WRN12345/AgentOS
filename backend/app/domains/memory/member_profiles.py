"""跨项目共享的成员文字档案服务。

档案由目标成员所在项目的负责人维护，写入后立即生效。档案归属于用户而非项目，
可跨项目读取，也对被评价者本人公开，避免形成不可见评价。
"""

import uuid

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE
from app.domains.memory.models import MemberProfile
from app.domains.memory.schemas import MemberProfileOut
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.project.service import get_member_by_user
from app.domains.work_items.schemas import MemberBrief
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")


async def get_profile(session: AsyncSession, user_id: uuid.UUID) -> MemberProfile | None:
    """按用户读取档案，不存在时返回 `None`。"""
    return (
        await session.execute(
            select(MemberProfile).where(MemberProfile.user_id == user_id)
        )
    ).scalar_one_or_none()


async def profile_to_out(
    session: AsyncSession, profile: MemberProfile, *, project_id: uuid.UUID | None = None
) -> MemberProfileOut:
    """序列化档案及创建者、最后编辑者信息。

    提供 `project_id` 时附带目标用户在该项目的成员状态；`None` 表示不是该项目成员。
    """
    creator = await session.get(ProjectMember, profile.created_by_member_id)
    editor = await session.get(ProjectMember, profile.last_edited_by_member_id)
    membership_active: bool | None = None
    if project_id is not None:
        target = await get_member_by_user(session, project_id, profile.user_id)
        membership_active = target.is_active if target is not None else None
    return MemberProfileOut(
        user_id=profile.user_id,
        content=profile.content,
        created_by=MemberBrief(
            id=profile.created_by_member_id,
            display_name=creator.display_name if creator else "",
        ),
        last_edited_by=MemberBrief(
            id=profile.last_edited_by_member_id,
            display_name=editor.display_name if editor else "",
        ),
        membership_active=membership_active,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def upsert_profile(
    session: AsyncSession, actor: ProjectMember, *, user_id: uuid.UUID, content: str
) -> MemberProfile:
    """创建或更新成员档案，提交后立即生效。

    `actor` 必须是当前项目负责人，目标用户必须属于同一项目。无项目成员身份的
    全局管理员不能通过此路径编辑档案。
    """
    if actor.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可编辑成员档案")
    target_user = await session.get(User, user_id)
    target_member = await get_member_by_user(session, actor.project_id, user_id)
    if target_user is None or target_member is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "目标用户不存在或不是本项目成员")

    content = content.strip()
    if not content:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "档案内容不能为空")

    profile = await get_profile(session, user_id)
    if profile is None:
        profile = MemberProfile(
            user_id=user_id,
            content=content,
            created_by_member_id=actor.id,
            last_edited_by_member_id=actor.id,
        )
        session.add(profile)
        await session.flush()
        await record_event(
            session,
            action="member_profile.created",
            actor_id=actor.user_id,
            target_type="member_profile",
            target_id=profile.id,
            after={"user_id": str(user_id), "content": content},
            project_id=actor.project_id,
        )
    else:
        before = {"content": profile.content}
        profile.content = content
        profile.last_edited_by_member_id = actor.id
        await record_event(
            session,
            action="member_profile.updated",
            actor_id=actor.user_id,
            target_type="member_profile",
            target_id=profile.id,
            before=before,
            after={"content": content},
            project_id=actor.project_id,
        )
    await session.commit()
    # `onupdate` 会使属性过期；异步会话无法隐式重载，序列化前必须显式刷新
    await session.refresh(profile)
    # 档案索引按来源整体重建；投递失败不回滚已提交的档案写入
    await _dispatch_profile_index(profile)
    return profile


async def _dispatch_profile_index(profile: MemberProfile) -> None:
    """投递档案索引任务；失败时仅记录日志。"""
    redis_client: redis.Redis = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_INDEX_TASK_TYPE,
            {
                "project_id": None,  # `profile` 归属于用户，不绑定项目
                "source_type": "profile",
                "source_id": str(profile.id),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "profile index task enqueue failed: profile=%s", profile.id, exc_info=True
        )
    finally:
        await redis_client.aclose()
