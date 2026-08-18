"""请求上下文：request_id、来源 IP 与项目归属。

由 RequestContextMiddleware 在每个请求进入时写入 contextvars，
日志、错误响应与审计事件（domains/audit）从中自动读取，业务代码无需手传。
project_id 从 X-Project-Id 请求头快照捕获（ticket 07，审计事件项目归属）；
无项目上下文的全局接口（登录/登出/管理控制台）记为 None。
"""

import uuid
from contextvars import ContextVar

from starlette.requests import Request

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_client_ip_var: ContextVar[str] = ContextVar("client_ip", default="")
_project_id_var: ContextVar[uuid.UUID | None] = ContextVar("project_id", default=None)


def set_request_context(
    request_id: str, client_ip: str, project_id: uuid.UUID | None = None
) -> None:
    _request_id_var.set(request_id)
    _client_ip_var.set(client_ip)
    _project_id_var.set(project_id)


def get_request_id() -> str:
    return _request_id_var.get()


def get_client_ip() -> str:
    return _client_ip_var.get()


def get_project_id() -> uuid.UUID | None:
    return _project_id_var.get()


def project_id_from_header(request: Request) -> uuid.UUID | None:
    """从 X-Project-Id 头宽松解析项目归属：缺失/无效 → None（全局接口语义）。

    供 RequestContextMiddleware（审计上下文快照）与 IdempotencyMiddleware
    （幂等键项目维度）共用，避免两处重复的宽松解析。
    注意与 project/dependencies.project_id_from_request（严格，无效 → 400）区分：
    本函数是快照/降级用，不负责请求校验。
    """
    raw = request.headers.get("X-Project-Id", "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
