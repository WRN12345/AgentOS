"""协作请求状态机（8.2 节）：纯函数，可独立单测。

合法迁移（与 8.2 节状态图一一对应）：
    REQUESTED          → ACCEPTED           接收人接受（accept）
    REQUESTED          → DECLINED           接收人拒绝（decline）
    REQUESTED          → CANCELLED          发起人取消（cancel）
    ACCEPTED           → IN_PROGRESS        接收人开始处理（start）
    ACCEPTED           → CANCELLED          双方确认取消（cancel）
    IN_PROGRESS        → SUBMITTED          回传产物（submit）
    SUBMITTED          → REVISION_REQUESTED 发起人要求修改（request_revision）
    REVISION_REQUESTED → IN_PROGRESS        接收人继续处理（start）
    SUBMITTED          → COMPLETED          发起人接受（complete）

取舍说明：
- 8.2 节"ACCEPTED → CANCELLED：双方确认取消"首版简化为发起人或接收人
  单方即可取消（权限在应用服务层校验），不引入二次确认往返；
- "REVISION_REQUESTED → IN_PROGRESS：继续处理"复用 start 命令，
  不再单独引入 resume 命令，端点与"开始处理"共用（12.4 节未单列）。

角色权限不在本模块：状态机只管"状态能否迁移"，"谁可以触发"
由应用服务层（domains/collaboration/service.py）显式校验（16 节）。
非法迁移一律抛 ApiException（COLLABORATION_INVALID_TRANSITION，409）。
"""

from enum import StrEnum

from app.core.errors import ApiException, ErrorCodes


class CollaborationStatus(StrEnum):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# 命令 → （允许的源状态集合，目标状态）
_TRANSITIONS: dict[str, tuple[frozenset[CollaborationStatus], CollaborationStatus]] = {
    "accept": (frozenset({CollaborationStatus.REQUESTED}), CollaborationStatus.ACCEPTED),
    "decline": (frozenset({CollaborationStatus.REQUESTED}), CollaborationStatus.DECLINED),
    "start": (
        frozenset({CollaborationStatus.ACCEPTED, CollaborationStatus.REVISION_REQUESTED}),
        CollaborationStatus.IN_PROGRESS,
    ),
    "submit": (frozenset({CollaborationStatus.IN_PROGRESS}), CollaborationStatus.SUBMITTED),
    "request_revision": (
        frozenset({CollaborationStatus.SUBMITTED}),
        CollaborationStatus.REVISION_REQUESTED,
    ),
    "complete": (frozenset({CollaborationStatus.SUBMITTED}), CollaborationStatus.COMPLETED),
    "cancel": (
        frozenset({CollaborationStatus.REQUESTED, CollaborationStatus.ACCEPTED}),
        CollaborationStatus.CANCELLED,
    ),
}

COMMANDS = tuple(_TRANSITIONS.keys())


def transition(current: str | CollaborationStatus, command: str) -> CollaborationStatus:
    """按命令推进状态；非法迁移抛 409 COLLABORATION_INVALID_TRANSITION。"""
    try:
        current_status = CollaborationStatus(current)
    except ValueError:
        raise ApiException(
            500, ErrorCodes.INTERNAL_ERROR, f"未知协作请求状态: {current}"
        ) from None

    rule = _TRANSITIONS.get(command)
    if rule is None:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, f"未知协作请求命令: {command}"
        )
    allowed_from, target = rule
    if current_status not in allowed_from:
        raise ApiException(
            409,
            ErrorCodes.COLLABORATION_INVALID_TRANSITION,
            f"当前状态 {current_status.value} 不允许执行 {command}",
            details={"current_status": current_status.value, "command": command},
        )
    return target
