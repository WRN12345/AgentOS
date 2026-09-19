import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException
from app.domains.handoffs.models import DeliverableHandoff


async def ensure_no_pending_handoff(session: AsyncSession, item_id: uuid.UUID) -> None:
    """Call while holding the work-item row lock, before mutating either endpoint."""
    pending = await session.scalar(
        select(DeliverableHandoff.id)
        .where(
            DeliverableHandoff.status == "pending",
            or_(
                DeliverableHandoff.source_work_item_id == item_id,
                DeliverableHandoff.target_work_item_id == item_id,
            ),
        )
        .limit(1)
    )
    if pending is not None:
        raise ApiException(409, "HANDOFF_PENDING_CONFLICT", "工作项存在待接收的移交")
