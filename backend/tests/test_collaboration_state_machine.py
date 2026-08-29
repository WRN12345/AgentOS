"""协作请求状态机单元测试。

覆盖全部合法迁移与代表性非法迁移；状态机为纯函数，不依赖数据库。
"""

import pytest

from app.core.errors import ApiException
from app.domains.collaboration.state_machine import COMMANDS, CollaborationStatus, transition

LEGAL_TRANSITIONS = [
    ("REQUESTED", "accept", "ACCEPTED"),
    ("REQUESTED", "decline", "DECLINED"),
    ("REQUESTED", "cancel", "CANCELLED"),
    ("ACCEPTED", "start", "IN_PROGRESS"),
    ("ACCEPTED", "cancel", "CANCELLED"),
    ("IN_PROGRESS", "submit", "SUBMITTED"),
    ("SUBMITTED", "request_revision", "REVISION_REQUESTED"),
    ("SUBMITTED", "complete", "COMPLETED"),
    ("REVISION_REQUESTED", "start", "IN_PROGRESS"),
]

# 代表性非法迁移：(当前状态, 命令)
ILLEGAL_TRANSITIONS = [
    ("REQUESTED", "start"),  # 未接受不能开始
    ("REQUESTED", "submit"),  # 未开始不能提交
    ("REQUESTED", "complete"),
    ("ACCEPTED", "accept"),  # 重复接受
    ("ACCEPTED", "submit"),
    ("IN_PROGRESS", "accept"),
    ("IN_PROGRESS", "complete"),  # 未提交不能完成
    ("IN_PROGRESS", "cancel"),  # 进行中不可取消（8.2 仅 REQUESTED/ACCEPTED 可取消）
    ("SUBMITTED", "submit"),  # 重复提交
    ("SUBMITTED", "cancel"),
    ("REVISION_REQUESTED", "submit"),  # 须先 start 继续处理
    ("REVISION_REQUESTED", "complete"),
    ("COMPLETED", "cancel"),  # 终态不可再迁移
    ("DECLINED", "accept"),  # 终态不可再迁移
    ("CANCELLED", "start"),  # 终态不可再迁移
]


@pytest.mark.parametrize(("current", "command", "expected"), LEGAL_TRANSITIONS)
def test_legal_transitions(current: str, command: str, expected: str) -> None:
    assert transition(current, command) == CollaborationStatus(expected)


@pytest.mark.parametrize(("current", "command"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_rejected(current: str, command: str) -> None:
    with pytest.raises(ApiException) as exc_info:
        transition(current, command)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "COLLABORATION_INVALID_TRANSITION"
    assert exc_info.value.details["current_status"] == current
    assert exc_info.value.details["command"] == command


def test_unknown_command_rejected() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("REQUESTED", "explode")
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "VALIDATION_ERROR"


def test_unknown_status_is_internal_error() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition("BOGUS", "accept")
    assert exc_info.value.status_code == 500


def test_all_declared_commands_have_transitions() -> None:
    assert set(COMMANDS) == {
        "accept",
        "decline",
        "start",
        "submit",
        "request_revision",
        "complete",
        "cancel",
    }
