"""请求上下文：request_id、来源 IP 与项目归属。

`RequestContextMiddleware` 在请求进入时写入 contextvars，供日志、错误响应和
审计事件读取。`project_id` 从 `X-Project-Id` 请求头捕获；登录、登出和管理控制台
等全局接口没有项目上下文，记为 `None`。
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
    """宽松解析 `X-Project-Id`；缺失或无效时返回 `None`。

    该结果仅用于审计快照和幂等键的项目维度，不负责请求校验。需要严格校验时应使用
    `project_id_from_request`，由其对无效值返回 400。
    """
    raw = request.headers.get("X-Project-Id", "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
