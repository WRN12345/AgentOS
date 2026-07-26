"""工作项状态机（8.1 节）：纯函数，可独立单测。

合法迁移（与 8.1 节状态图一一对应）：
    DRAFT       → READY        负责人发布（publish）
    READY       → IN_PROGRESS  主执行人开始（start）
    IN_PROGRESS → BLOCKED      标记阻塞（block）
    BLOCKED     → IN_PROGRESS  解除阻塞（unblock）
    IN_PROGRESS → IN_REVIEW    提交最终交付物（submit）
    IN_REVIEW   → IN_PROGRESS  负责人要求修改（request_changes，审核在阶段 4 接入）
    IN_REVIEW   → COMPLETED    负责人通过（complete，审核在阶段 4 接入）
    DRAFT/READY/IN_PROGRESS → CANCELLED  取消（cancel）

角色权限不在本模块：状态机只管"状态能否迁移"，"谁可以触发"
由应用服务层（domains/work_items/service.py）显式校验（16 节）。
非法迁移一律抛 ApiException（WORK_ITEM_INVALID_TRANSITION，409）。
"""

from enum import StrEnum

from app.core.errors import ApiException, ErrorCodes


class WorkItemStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    IN_REVIEW = "IN_REVIEW"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# 计入"当前有效任务负载"的进行中状态（6.2 节，供成员摘要统计）
ACTIVE_STATUSES: tuple[str, ...] = (
    WorkItemStatus.READY.value,
    WorkItemStatus.IN_PROGRESS.value,
    WorkItemStatus.BLOCKED.value,
    WorkItemStatus.IN_REVIEW.value,
)

# 命令 → （允许的源状态集合，目标状态）
_TRANSITIONS: dict[str, tuple[frozenset[WorkItemStatus], WorkItemStatus]] = {
    "publish": (frozenset({WorkItemStatus.DRAFT}), WorkItemStatus.READY),
    "start": (frozenset({WorkItemStatus.READY}), WorkItemStatus.IN_PROGRESS),
    "block": (frozenset({WorkItemStatus.IN_PROGRESS}), WorkItemStatus.BLOCKED),
    "unblock": (frozenset({WorkItemStatus.BLOCKED}), WorkItemStatus.IN_PROGRESS),
    "submit": (frozenset({WorkItemStatus.IN_PROGRESS}), WorkItemStatus.IN_REVIEW),
    "request_changes": (frozenset({WorkItemStatus.IN_REVIEW}), WorkItemStatus.IN_PROGRESS),
    "complete": (frozenset({WorkItemStatus.IN_REVIEW}), WorkItemStatus.COMPLETED),
    "cancel": (
        frozenset(
            {WorkItemStatus.DRAFT, WorkItemStatus.READY, WorkItemStatus.IN_PROGRESS}
        ),
        WorkItemStatus.CANCELLED,
    ),
}

COMMANDS = tuple(_TRANSITIONS.keys())


def transition(current: str | WorkItemStatus, command: str) -> WorkItemStatus:
    """按命令推进状态；非法迁移抛 409 WORK_ITEM_INVALID_TRANSITION。"""
    try:
        current_status = WorkItemStatus(current)
    except ValueError:
        raise ApiException(
            500, ErrorCodes.INTERNAL_ERROR, f"未知工作项状态: {current}"
        ) from None

    rule = _TRANSITIONS.get(command)
    if rule is None:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, f"未知工作项命令: {command}"
        )
    allowed_from, target = rule
    if current_status not in allowed_from:
        raise ApiException(
            409,
            ErrorCodes.WORK_ITEM_INVALID_TRANSITION,
            f"当前状态 {current_status.value} 不允许执行 {command}",
            details={"current_status": current_status.value, "command": command},
        )
    return target
