"""成员文字档案（设计文档第 7 节②，M3.5）。

- 负责人维护、写完直接生效（不走确认流程）；记录统计体现不出来的信息；
- 编辑权（15.6 默认规则）：该成员所属任一项目的负责人均可编辑——
  本期实现为"当前项目上下文中，负责人编辑本项目成员的档案"，
  跨项目负责人在非所属项目上下文编辑的细则实现前确认；
- 可读性（16.1 + 第 12 节例外）：项目内全员可读，含被评价者本人；
  档案随人走，跨项目可读（不做暗箱评价，也服务跨项目分配决策）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.identity.models import User
from app.domains.memory.models import MemberProfile
from app.domains.memory.schemas import MemberProfileOut
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.project.service import get_member_by_user
from app.domains.work_items.schemas import MemberBrief


async def get_profile(session: AsyncSession, user_id: uuid.UUID) -> MemberProfile | None:
    """按用户取档案；不存在返回 None（新成员尚无档案属正常）。"""
    return (
        await session.execute(
            select(MemberProfile).where(MemberProfile.user_id == user_id)
        )
    ).scalar_one_or_none()


async def profile_to_out(
    session: AsyncSession, profile: MemberProfile
) -> MemberProfileOut:
    """档案序列化：创建/编辑者显示名（project_members 可能跨项目，按 id 直取）。"""
    creator = await session.get(ProjectMember, profile.created_by_member_id)
    editor = await session.get(ProjectMember, profile.last_edited_by_member_id)
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
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def upsert_profile(
    session: AsyncSession, actor: ProjectMember, *, user_id: uuid.UUID, content: str
) -> MemberProfile:
    """创建或更新成员档案（写完直接生效，第 7 节）。

    权限：actor 为当前项目负责人，且目标用户是该项目成员（15.6 默认规则的
    本期实现）；全局 admin 无成员身份，不经此路径编辑（第 12 节）。
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
    else:
        profile.content = content
        profile.last_edited_by_member_id = actor.id
    await session.flush()
    await session.commit()
    # updated_at 的 onupdate=func.now() 在 UPDATE 后被 SQLAlchemy 标记过期，
    # 异步会话属性访问无法隐式重载（MissingGreenlet），序列化前显式刷新
    await session.refresh(profile)
    return profile
