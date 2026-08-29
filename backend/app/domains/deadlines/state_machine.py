"""可独立测试的 DDL 变更申请状态机。

合法迁移：
    PENDING_IMPACT_ANALYSIS → PENDING_APPROVAL  影响分析完成（analyze）
    PENDING_APPROVAL        → APPROVED          负责人审批通过 / 协作级自动生效（approve）
    PENDING_APPROVAL        → REJECTED          负责人驳回（reject）
    PENDING_IMPACT_ANALYSIS → CANCELLED         发起人取消（cancel）
    PENDING_APPROVAL        → CANCELLED         发起人取消（cancel）

规则化影响分析在创建申请的事务内同步执行 `analyze` 并推进到 `PENDING_APPROVAL`。
`PENDING_IMPACT_ANALYSIS` 也可供异步分析使用；分析失败并标记
`impact_analysis_status=unavailable` 时仍允许人工审批。

角色权限不在本模块：状态机只管"状态能否迁移"，"谁可以触发"
由应用服务显式校验。非法迁移抛出 `ApiException`，错误码为
`DEADLINE_CHANGE_INVALID_TRANSITION`，HTTP 状态为 `409`。
"""

from enum import StrEnum

from app.core.errors import ApiException, ErrorCodes


class DeadlineChangeStatus(StrEnum):
    PENDING_IMPACT_ANALYSIS = "PENDING_IMPACT_ANALYSIS"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# 待审批状态集合由唯一性约束和审批聚合共同使用。
PENDING_STATUSES = frozenset(
    {
        DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS,
        DeadlineChangeStatus.PENDING_APPROVAL,
    }
)


class ImpactAnalysisStatus(StrEnum):
    GENERATED = "generated"
    UNAVAILABLE = "unavailable"


class DeadlineTargetType(StrEnum):
    WORK_ITEM = "work_item"
    COLLABORATION_REQUEST = "collaboration_request"


_TRANSITIONS: dict[str, tuple[frozenset[DeadlineChangeStatus], DeadlineChangeStatus]] = {
    "analyze": (
        frozenset({DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS}),
        DeadlineChangeStatus.PENDING_APPROVAL,
    ),
    "approve": (
        frozenset({DeadlineChangeStatus.PENDING_APPROVAL}),
        DeadlineChangeStatus.APPROVED,
    ),
    "reject": (
        frozenset({DeadlineChangeStatus.PENDING_APPROVAL}),
        DeadlineChangeStatus.REJECTED,
    ),
    "cancel": (
        frozenset(
            {
                DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS,
                DeadlineChangeStatus.PENDING_APPROVAL,
            }
        ),
        DeadlineChangeStatus.CANCELLED,
    ),
}

COMMANDS = tuple(_TRANSITIONS.keys())


def transition(current: str | DeadlineChangeStatus, command: str) -> DeadlineChangeStatus:
    """按命令推进状态；非法迁移抛出 `ApiException`，由接口映射为 `409`。"""
    try:
        current_status = DeadlineChangeStatus(current)
    except ValueError:
        raise ApiException(
            500, ErrorCodes.INTERNAL_ERROR, f"未知 DDL 变更申请状态: {current}"
        ) from None

    rule = _TRANSITIONS.get(command)
    if rule is None:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, f"未知 DDL 变更命令: {command}")
    allowed_from, target = rule
    if current_status not in allowed_from:
        raise ApiException(
            409,
            ErrorCodes.DEADLINE_CHANGE_INVALID_TRANSITION,
            f"当前状态 {current_status.value} 不允许执行 {command}",
            details={"current_status": current_status.value, "command": command},
        )
    return target
