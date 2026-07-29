"""统一 API 错误格式（17.1 节）与业务异常定义。

所有错误响应统一为 {"code", "message", "request_id", "details"} 结构，
request_id 由请求上下文中间件生成（见 core/request_context.py）。
"""

from typing import Any


class ErrorCodes:
    """错误码常量。

    版本冲突等机制在本批定义，正式启用在 T2.5/T2.6。
    """

    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # 同一幂等键的并发请求等待首次响应超时（17.2 节）
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USER_DISABLED = "USER_DISABLED"
    INVALID_TOKEN = "INVALID_TOKEN"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"

    # 项目成员与权限（6.1 节）：资源关系校验失败统一 403
    FORBIDDEN = "FORBIDDEN"
    NOT_PROJECT_MEMBER = "NOT_PROJECT_MEMBER"
    USERNAME_TAKEN = "USERNAME_TAKEN"

    # 乐观锁版本冲突（17.2 节）：更新接口携带 version，不匹配返回 409
    WORK_ITEM_VERSION_CONFLICT = "WORK_ITEM_VERSION_CONFLICT"
    # 工作项非法状态迁移（8.1 节）
    WORK_ITEM_INVALID_TRANSITION = "WORK_ITEM_INVALID_TRANSITION"

    # 协作请求乐观锁版本冲突（17.2 节）
    COLLABORATION_VERSION_CONFLICT = "COLLABORATION_VERSION_CONFLICT"
    # 协作请求非法状态迁移（8.2 节）
    COLLABORATION_INVALID_TRANSITION = "COLLABORATION_INVALID_TRANSITION"

    # 转派申请乐观锁版本冲突（17.2 节）
    TRANSFER_VERSION_CONFLICT = "TRANSFER_VERSION_CONFLICT"
    # 转派申请非法状态迁移（8.3 节）
    TRANSFER_INVALID_TRANSITION = "TRANSFER_INVALID_TRANSITION"
    # 同一工作项已存在待审批转派申请（8.3、17.2 节）
    TRANSFER_PENDING_CONFLICT = "TRANSFER_PENDING_CONFLICT"

    # DDL 变更申请乐观锁版本冲突（17.2 节）
    DEADLINE_CHANGE_VERSION_CONFLICT = "DEADLINE_CHANGE_VERSION_CONFLICT"
    # DDL 变更申请非法状态迁移（8.4 节）
    DEADLINE_CHANGE_INVALID_TRANSITION = "DEADLINE_CHANGE_INVALID_TRANSITION"
    # 同一工作项已存在待审批主 DDL 变更（7.4、17.2 节）
    DEADLINE_CHANGE_PENDING_CONFLICT = "DEADLINE_CHANGE_PENDING_CONFLICT"

    # 文件上传（第 14 章）：超过配置大小上限 / 扩展名或 MIME 不在白名单
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"

    # 提交审核前须已存在交付物（7.5 节，T4.4）
    DELIVERABLE_REQUIRED = "DELIVERABLE_REQUIRED"
    # 交付物版本号并发冲突（(work_item_id, version) 唯一约束兜底，17.2 节）
    DELIVERABLE_VERSION_CONFLICT = "DELIVERABLE_VERSION_CONFLICT"

    # Agent 运行人工重试（17.3 节，T5.6）：仅 failed 可重试，其余状态 409
    AGENT_RUN_NOT_FAILED = "AGENT_RUN_NOT_FAILED"
    # Agent 建议人工反馈（12.5 节，T5.7）：仅 pending 可反馈，重复反馈 409
    AGENT_SUGGESTION_ALREADY_REVIEWED = "AGENT_SUGGESTION_ALREADY_REVIEWED"


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
    """幂等命中：不重复执行业务写入，直接返回首次响应（17.2 节）。"""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__("idempotent replay")
        self.status_code = status_code
        self.body = body
