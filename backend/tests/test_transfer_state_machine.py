"""转派申请状态机单元测试（8.3 节，T3.3 验收）。

覆盖全部合法迁移与代表性非法迁移；状态机为纯函数，不依赖数据库。
"""

import pytest

from app.core.errors import ApiException
from app.domains.transfers.state_machine import COMMANDS, TransferStatus, transition

# 8.3 节全部合法迁移：(当前状态, 命令, 目标状态)
LEGAL_TRANSITIONS = [
    ("PENDING", "approve", "APPROVED"),
    ("PENDING", "reject", "REJECTED"),
    ("PENDING", "cancel", "CANCELLED"),
]

# 全部非法迁移：(当前状态, 命令)
ILLEGAL_TRANSITIONS = [
    ("APPROVED", "approve"),  # 终态不可再迁移
    ("APPROVED", "reject"),
    ("APPROVED", "cancel"),
    ("REJECTED", "approve"),
    ("REJECTED", "reject"),
    ("REJECTED", "cancel"),
    ("CANCELLED", "approve"),
    ("CANCELLED", "reject"),
    ("CANCELLED", "cancel"),
]


@pytest.mark.parametrize(("current", "command", "expected"), LEGAL_TRANSITIONS)
def test_legal_transitions(current: str, command: str, expected: str) -> None:
    assert transition(current, command) == TransferStatus(expected)


@pytest.mark.parametrize(("current", "command"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_rejected(current: str, command: str) -> None:
    with pytest.raises(ApiException) as exc_info:
        transition(current, command)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "TRANSFER_INVALID_TRANSITION"
    assert exc_info.value.details["current_status"] == current
    assert exc_info.value.details["command"] == command


def test_unknown_command_rejected() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("PENDING", "explode")
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "VALIDATION_ERROR"


def test_unknown_status_raises_internal_error() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("NOPE", "approve")
    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "INTERNAL_ERROR"


def test_commands_cover_all() -> None:
    assert set(COMMANDS) == {"approve", "reject", "cancel"}
