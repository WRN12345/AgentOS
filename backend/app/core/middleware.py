"""请求上下文中间件：为每个请求生成 request_id 并透传。

- 生成 UUID 作为 request_id，写入 contextvars（日志/错误响应/审计事件自动取用）
  和响应头 X-Request-ID；
- 记录来源 IP 到上下文（审计事件使用，第 16 章）；
- 从 X-Project-Id 请求头快照项目归属到上下文（ticket 07：审计事件项目归属，
  落库时捕获；缺失/无效头记为 None，即全局接口语义）。
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
