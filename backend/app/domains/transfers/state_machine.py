"""转派申请状态机（8.3 节）：纯函数，可独立单测。

合法迁移（与 8.3 节一一对应）：
    PENDING → APPROVED   负责人审批通过（approve）
    PENDING → REJECTED   负责人驳回（reject）
    PENDING → CANCELLED  发起人取消（cancel）

角色权限不在本模块：状态机只管"状态能否迁移"，"谁可以触发"
由应用服务层（domains/transfers/service.py）显式校验（16 节）。
非法迁移一律抛 ApiException（TRANSFER_INVALID_TRANSITION，409）。
"""

from enum import StrEnum

from app.core.errors import ApiException, ErrorCodes


class TransferStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# 命令 → （允许的源状态集合，目标状态）
_TRANSITIONS: dict[str, tuple[frozenset[TransferStatus], TransferStatus]] = {
    "approve": (frozenset({TransferStatus.PENDING}), TransferStatus.APPROVED),
    "reject": (frozenset({TransferStatus.PENDING}), TransferStatus.REJECTED),
    "cancel": (frozenset({TransferStatus.PENDING}), TransferStatus.CANCELLED),
}

COMMANDS = tuple(_TRANSITIONS.keys())


def transition(current: str | TransferStatus, command: str) -> TransferStatus:
    """按命令推进状态；非法迁移抛 409 TRANSFER_INVALID_TRANSITION。"""
    try:
        current_status = TransferStatus(current)
    except ValueError:
        raise ApiException(
            500, ErrorCodes.INTERNAL_ERROR, f"未知转派申请状态: {current}"
        ) from None

    rule = _TRANSITIONS.get(command)
    if rule is None:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, f"未知转派申请命令: {command}")
    allowed_from, target = rule
    if current_status not in allowed_from:
        raise ApiException(
            409,
            ErrorCodes.TRANSFER_INVALID_TRANSITION,
            f"当前状态 {current_status.value} 不允许执行 {command}",
            details={"current_status": current_status.value, "command": command},
        )
    return target
