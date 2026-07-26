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
