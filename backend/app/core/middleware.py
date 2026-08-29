"""请求上下文中间件：为每个请求生成并透传 `request_id`。

中间件把 `request_id`、来源 IP 和 `X-Project-Id` 快照写入 contextvars，供日志、
错误响应和审计事件使用，并在响应头 `X-Request-ID` 中返回请求标识。缺失或无效的
项目头记为 `None`，表示全局接口。
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.request_context import project_id_from_header, set_request_context


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        client_ip = request.client.host if request.client else ""
        set_request_context(request_id, client_ip, project_id_from_header(request))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
