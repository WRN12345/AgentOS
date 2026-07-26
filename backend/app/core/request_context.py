"""请求上下文：request_id 与来源 IP。

由 RequestContextMiddleware 在每个请求进入时写入 contextvars，
日志、错误响应与审计事件（domains/audit）从中自动读取，业务代码无需手传。
"""

from contextvars import ContextVar

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_client_ip_var: ContextVar[str] = ContextVar("client_ip", default="")


def set_request_context(request_id: str, client_ip: str) -> None:
    _request_id_var.set(request_id)
    _client_ip_var.set(client_ip)


def get_request_id() -> str:
    return _request_id_var.get()


def get_client_ip() -> str:
    return _client_ip_var.get()
