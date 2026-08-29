"""统一 API 错误格式与业务异常定义。

所有错误响应统一为 {"code", "message", "request_id", "details"} 结构，
request_id 由请求上下文中间件生成（见 core/request_context.py）。
"""

from typing import Any


class ErrorCodes:
    """API 错误码。"""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # 同一幂等键的并发请求等待首次响应超时。
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USER_DISABLED = "USER_DISABLED"
    INVALID_TOKEN = "INVALID_TOKEN"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"

    # 项目成员或资源关系校验失败统一返回 403。
    FORBIDDEN = "FORBIDDEN"
    NOT_PROJECT_MEMBER = "NOT_PROJECT_MEMBER"
    MISSING_PROJECT_ID = "MISSING_PROJECT_ID"
    USERNAME_TAKEN = "USERNAME_TAKEN"
    PROJECT_LEADER_REQUIRED = "PROJECT_LEADER_REQUIRED"
    PROJECT_MEMBER_DISABLED = "PROJECT_MEMBER_DISABLED"

    # 跨项目指派成员属于非法引用，返回 400。
    CROSS_PROJECT_REFERENCE = "CROSS_PROJECT_REFERENCE"

    # 工作项乐观锁版本不匹配时返回 409。
    WORK_ITEM_VERSION_CONFLICT = "WORK_ITEM_VERSION_CONFLICT"
    WORK_ITEM_INVALID_TRANSITION = "WORK_ITEM_INVALID_TRANSITION"

    COLLABORATION_VERSION_CONFLICT = "COLLABORATION_VERSION_CONFLICT"
    COLLABORATION_INVALID_TRANSITION = "COLLABORATION_INVALID_TRANSITION"

    TRANSFER_VERSION_CONFLICT = "TRANSFER_VERSION_CONFLICT"
    TRANSFER_INVALID_TRANSITION = "TRANSFER_INVALID_TRANSITION"
    # 同一工作项只能有一个待审批转派申请。
    TRANSFER_PENDING_CONFLICT = "TRANSFER_PENDING_CONFLICT"

    DEADLINE_CHANGE_VERSION_CONFLICT = "DEADLINE_CHANGE_VERSION_CONFLICT"
    DEADLINE_CHANGE_INVALID_TRANSITION = "DEADLINE_CHANGE_INVALID_TRANSITION"
    # 同一工作项只能有一个待审批的主 DDL 变更。
    DEADLINE_CHANGE_PENDING_CONFLICT = "DEADLINE_CHANGE_PENDING_CONFLICT"

    # 上传文件不得超过大小上限，扩展名和 MIME 必须在白名单内。
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"
    # 唯一约束 `ux_stored_files_current_name` 兜底处理同名版本链并发冲突。
    FILE_VERSION_CONFLICT = "FILE_VERSION_CONFLICT"
    FILE_INDEX_INVALID_TRANSITION = "FILE_INDEX_INVALID_TRANSITION"

    # 提交审核前必须存在交付物。
    DELIVERABLE_REQUIRED = "DELIVERABLE_REQUIRED"
    # `(work_item_id, version)` 唯一约束兜底处理版本号并发冲突。
    DELIVERABLE_VERSION_CONFLICT = "DELIVERABLE_VERSION_CONFLICT"

    # 仅 `failed` 状态的 Agent 运行可人工重试，否则返回 409。
    AGENT_RUN_NOT_FAILED = "AGENT_RUN_NOT_FAILED"
    # 仅 `pending` 建议可反馈，重复反馈返回 409。
    AGENT_SUGGESTION_ALREADY_REVIEWED = "AGENT_SUGGESTION_ALREADY_REVIEWED"

    # 开发文档的启动前置校验、乐观锁和状态迁移错误。
    DEV_DOC_REQUIRED = "DEV_DOC_REQUIRED"
    DEV_DOC_VERSION_CONFLICT = "DEV_DOC_VERSION_CONFLICT"
    DEV_DOC_INVALID_TRANSITION = "DEV_DOC_INVALID_TRANSITION"

    # 核心记忆容量超限和非法状态迁移。
    CORE_MEMORY_BUDGET_EXCEEDED = "CORE_MEMORY_BUDGET_EXCEEDED"
    CORE_MEMORY_INVALID_TRANSITION = "CORE_MEMORY_INVALID_TRANSITION"


class ApiException(Exception):
    """业务异常：按统一错误格式返回。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class IdempotentReplay(Exception):
    """幂等命中时携带首次响应，避免重复执行业务写入。"""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__("idempotent replay")
        self.status_code = status_code
        self.body = body
