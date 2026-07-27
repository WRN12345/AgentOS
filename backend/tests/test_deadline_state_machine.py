"""DDL 变更申请状态机单元测试（8.4 节，T3.4 验收）。

覆盖全部合法迁移与代表性非法迁移；状态机为纯函数，不依赖数据库。
"""

import pytest

from app.core.errors import ApiException
from app.domains.deadlines.state_machine import COMMANDS, DeadlineChangeStatus, transition

# 8.4 节全部合法迁移：(当前状态, 命令, 目标状态)
LEGAL_TRANSITIONS = [
    ("PENDING_IMPACT_ANALYSIS", "analyze", "PENDING_APPROVAL"),
    ("PENDING_IMPACT_ANALYSIS", "cancel", "CANCELLED"),
    ("PENDING_APPROVAL", "approve", "APPROVED"),
    ("PENDING_APPROVAL", "reject", "REJECTED"),
    ("PENDING_APPROVAL", "cancel", "CANCELLED"),
]

# 代表性非法迁移：(当前状态, 命令)
ILLEGAL_TRANSITIONS = [
    ("PENDING_IMPACT_ANALYSIS", "approve"),  # 须先完成影响分析
    ("PENDING_IMPACT_ANALYSIS", "reject"),
    ("PENDING_APPROVAL", "analyze"),  # 不可重复分析
    ("APPROVED", "approve"),  # 终态不可再迁移
    ("APPROVED", "reject"),
    ("APPROVED", "cancel"),
    ("REJECTED", "approve"),
    ("REJECTED", "analyze"),
    ("CANCELLED", "approve"),
    ("CANCELLED", "cancel"),
]


@pytest.mark.parametrize(("current", "command", "expected"), LEGAL_TRANSITIONS)
def test_legal_transitions(current: str, command: str, expected: str) -> None:
    assert transition(current, command) == DeadlineChangeStatus(expected)


@pytest.mark.parametrize(("current", "command"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_rejected(current: str, command: str) -> None:
    with pytest.raises(ApiException) as exc_info:
        transition(current, command)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "DEADLINE_CHANGE_INVALID_TRANSITION"
    assert exc_info.value.details["current_status"] == current
    assert exc_info.value.details["command"] == command


def test_unknown_command_rejected() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("PENDING_APPROVAL", "explode")
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "VALIDATION_ERROR"


def test_unknown_status_raises_internal_error() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("NOPE", "analyze")
    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "INTERNAL_ERROR"


def test_commands_cover_all() -> None:
    assert set(COMMANDS) == {"analyze", "approve", "reject", "cancel"}
