"""可独立测试的开发文档状态机。

合法迁移：
    DRAFT     → SUBMITTED  主执行人提交审核（submit）
    RETURNED  → SUBMITTED  打回后修改重新提交（submit）
    SUBMITTED → CONFIRMED  负责人确认通过（confirm）
    SUBMITTED → RETURNED   负责人打回（return）

角色权限不在本模块：状态机只管"状态能否迁移"，"谁可以触发"
由 `domains/dev_docs/service.py` 中的应用服务显式校验。
非法迁移抛出 `ApiException`，错误码为 `DEV_DOC_INVALID_TRANSITION`，HTTP 状态为
`409`。`waive` 不是状态迁移，不改变文档状态。
"""

from enum import StrEnum

from app.core.errors import ApiException, ErrorCodes


class DevDocStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    RETURNED = "RETURNED"


_TRANSITIONS: dict[str, tuple[frozenset[DevDocStatus], DevDocStatus]] = {
    "submit": (
        frozenset({DevDocStatus.DRAFT, DevDocStatus.RETURNED}),
        DevDocStatus.SUBMITTED,
    ),
    "confirm": (frozenset({DevDocStatus.SUBMITTED}), DevDocStatus.CONFIRMED),
    "return": (frozenset({DevDocStatus.SUBMITTED}), DevDocStatus.RETURNED),
}

COMMANDS = tuple(_TRANSITIONS.keys())


def transition(current: str | DevDocStatus, command: str) -> DevDocStatus:
    """按命令推进状态；非法迁移抛出 `ApiException`，由接口映射为 `409`。"""
    try:
        current_status = DevDocStatus(current)
    except ValueError:
        raise ApiException(
            500, ErrorCodes.INTERNAL_ERROR, f"未知开发文档状态: {current}"
        ) from None

    rule = _TRANSITIONS.get(command)
    if rule is None:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, f"未知开发文档命令: {command}")
    allowed_from, target = rule
    if current_status not in allowed_from:
        raise ApiException(
            409,
            ErrorCodes.DEV_DOC_INVALID_TRANSITION,
            f"当前状态 {current_status.value} 不允许执行 {command}",
            details={"current_status": current_status.value, "command": command},
        )
    return target
