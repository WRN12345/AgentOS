"""工作项状态机的全部合法迁移与非法迁移测试。

纯函数测试，不依赖数据库。
"""

import pytest

from app.core.errors import ApiException, ErrorCodes
from app.domains.work_items.state_machine import WorkItemStatus as S
from app.domains.work_items.state_machine import transition


@pytest.mark.parametrize(
    ("current", "command", "target"),
    [
        (S.DRAFT, "publish", S.READY),
        (S.READY, "start", S.IN_PROGRESS),
        (S.IN_PROGRESS, "block", S.BLOCKED),
        (S.BLOCKED, "unblock", S.IN_PROGRESS),
        (S.IN_PROGRESS, "submit", S.IN_REVIEW),
        (S.IN_REVIEW, "request_changes", S.IN_PROGRESS),
        (S.IN_REVIEW, "complete", S.COMPLETED),
        (S.DRAFT, "cancel", S.CANCELLED),
        (S.READY, "cancel", S.CANCELLED),
        (S.IN_PROGRESS, "cancel", S.CANCELLED),
    ],
)
def test_legal_transitions(current: S, command: str, target: S) -> None:
    assert transition(current, command) == target


@pytest.mark.parametrize(
    ("current", "command"),
    [
        # DRAFT 只允许 publish/cancel
        (S.DRAFT, "start"),
        (S.DRAFT, "block"),
        (S.DRAFT, "submit"),
        (S.DRAFT, "complete"),
        # READY 只允许 start/cancel
        (S.READY, "publish"),
        (S.READY, "block"),
        (S.READY, "unblock"),
        (S.READY, "submit"),
        # IN_PROGRESS 不允许直接发布/解除阻塞/完成
        (S.IN_PROGRESS, "publish"),
        (S.IN_PROGRESS, "unblock"),
        (S.IN_PROGRESS, "complete"),
        (S.IN_PROGRESS, "request_changes"),
        # BLOCKED 只允许 unblock
        (S.BLOCKED, "start"),
        (S.BLOCKED, "block"),
        (S.BLOCKED, "submit"),
        (S.BLOCKED, "cancel"),
        # IN_REVIEW 的状态变化只能由审核动作触发。
        (S.IN_REVIEW, "start"),
        (S.IN_REVIEW, "block"),
        (S.IN_REVIEW, "cancel"),
        # 终态不可再迁移
        (S.COMPLETED, "start"),
        (S.COMPLETED, "cancel"),
        (S.CANCELLED, "publish"),
        (S.CANCELLED, "start"),
    ],
)
def test_illegal_transitions_rejected(current: S, command: str) -> None:
    with pytest.raises(ApiException) as exc_info:
        transition(current, command)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCodes.WORK_ITEM_INVALID_TRANSITION


def test_unknown_command_rejected() -> None:
    with pytest.raises(ApiException) as exc_info:
        transition(S.DRAFT, "explode")
    assert exc_info.value.status_code == 400


def test_accepts_plain_string_status() -> None:
    """数据库取出的字符串状态同样可用。"""
    assert transition("DRAFT", "publish") == S.READY
