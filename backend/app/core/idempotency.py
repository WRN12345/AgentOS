"""Idempotency-Key 支持（12 章、17.2 节）。

命令类接口（POST/PATCH 等状态变更接口）在签名中声明 `Depends(idempotency_guard)`
即可启用幂等：携带 `Idempotency-Key` 的重复请求不重复执行业务写入，
直接返回首次响应（响应头 `Idempotency-Replayed: true`）。

机制分两半：
- `idempotency_guard` 依赖项：查 `idempotency_records` 表，命中则抛
  `IdempotentReplay`（由全局异常处理返回首次响应），未命中则在 request.state
  登记待保存信息；
- `IdempotencyMiddleware`：请求成功后把首次响应（状态码 + body）落库。

注意：若接口同时使用 get_current_user，需把它声明在 idempotency_guard 之前，
以便守卫能取到 request.state.user_id（未登录接口 user_id 记为 NULL，
唯一约束按 COALESCE(user_id, 零值 UUID) 处理）。
"""

import json
from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.errors import IdempotentReplay
from app.core.logging import setup_logging
from app.infrastructure.database.engine import async_session_factory, get_session
from app.infrastructure.models.idempotency import IdempotencyRecord

logger = setup_logging("backend")


async def idempotency_guard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """命令接口幂等守卫依赖项。未携带 Idempotency-Key 时直接放行。"""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return

    user_id = getattr(request.state, "user_id", None)
    user_clause = (
        IdempotencyRecord.user_id.is_(None)
        if user_id is None
        else IdempotencyRecord.user_id == user_id
    )
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.key == key,
        IdempotencyRecord.method == request.method,
        IdempotencyRecord.path == request.url.path,
        user_clause,
    )
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is not None:
        raise IdempotentReplay(record.response_status, record.response_body)

    request.state.idempotency_pending = {
        "key": key,
        "user_id": user_id,
        "method": request.method,
        "path": request.url.path,
    }


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """请求成功后持久化首次响应，供幂等重放。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        pending: dict[str, Any] | None = getattr(request.state, "idempotency_pending", None)
        if pending is None or response.status_code >= 500:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            parsed: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"_raw": body.decode("utf-8", errors="replace")[:4096]}

        try:
            async with async_session_factory() as session:
                session.add(
                    IdempotencyRecord(
                        key=pending["key"],
                        user_id=pending["user_id"],
                        method=pending["method"],
                        path=pending["path"],
                        response_status=response.status_code,
                        response_body=parsed,
                    )
                )
                await session.commit()
        except IntegrityError:
            # 并发下同键请求已落库，本次响应照常返回，不覆盖首次记录
            logger.warning("idempotency record already saved concurrently: path=%s", pending["path"])

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
