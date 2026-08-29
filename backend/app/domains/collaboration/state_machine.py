"""可独立测试的协作请求状态机。

合法迁移：
    REQUESTED          → ACCEPTED           接收人接受（accept）
    REQUESTED          → DECLINED           接收人拒绝（decline）
    REQUESTED          → CANCELLED          发起人取消（cancel）
    ACCEPTED           → IN_PROGRESS        接收人开始处理（start）
    ACCEPTED           → CANCELLED          双方确认取消（cancel）
    IN_PROGRESS        → SUBMITTED          回传产物（submit）
    SUBMITTED          → REVISION_REQUESTED 发起人要求修改（request_revision）
    REVISION_REQUESTED → IN_PROGRESS        接收人继续处理（start）
    SUBMITTED          → COMPLETED          发起人接受（complete）

`ACCEPTED → CANCELLED` 允许任一方取消，不引入二次确认往返。
`REVISION_REQUESTED → IN_PROGRESS` 复用 `start`，不额外引入 `resume`。

状态机只判断状态能否迁移，角色权限由应用服务显式校验。非法迁移抛出
`ApiException`，错误码为 `COLLABORATION_INVALID_TRANSITION`，HTTP 状态为 `409`。
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
    """按命令推进状态；非法迁移抛出 `ApiException`，由接口映射为 `409`。"""
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
