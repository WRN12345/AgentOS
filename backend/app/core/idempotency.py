"""Idempotency-Key 支持（12 章、17.2 节）。

命令类接口（POST/PATCH 等状态变更接口）在签名中声明 `Depends(idempotency_guard)`
即可启用幂等：携带 `Idempotency-Key` 的重复请求不重复执行业务写入，
直接返回首次响应（响应头 `Idempotency-Replayed: true`）。

机制分两半：
- `idempotency_guard` 依赖项：先查 `idempotency_records`，命中已完成记录则抛
  `IdempotentReplay`（由全局异常处理返回首次响应）；未命中则以"占位记录"
  （response_status=0）抢占执行权——唯一索引 ux_idempotency_records_user_key_endpoint
  保证并发下同键只有一个请求占位成功，其余请求转入等待，待首个请求响应
  落库后按首次结果重放（17.2 节：并发重复请求也只生效一次）；
- `IdempotencyMiddleware`：请求结束后把首次响应（状态码 + body）写回占位
  记录；响应为 5xx 或抛出未处理异常时删除占位，允许后续请求重新执行。

注意：若接口同时使用 get_current_user，需把它声明在 idempotency_guard 之前，
以便守卫能取到 request.state.user_id（未登录接口 user_id 记为 NULL，
唯一约束按 COALESCE(user_id, 零值 UUID) 处理）。
"""

import asyncio
import json
import time
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.errors import ApiException, ErrorCodes, IdempotentReplay
from app.core.logging import setup_logging
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.idempotency import IdempotencyRecord

logger = setup_logging("backend")

#: 占位记录的 response_status：执行权已被抢占，但首次响应尚未落库
RESERVED_STATUS = 0
#: 并发请求等待首次响应的最长时间与轮询间隔（兜底，正常路径远快于此）
WAIT_TIMEOUT_SECONDS = 10.0
WAIT_INTERVAL_SECONDS = 0.05


def _user_clause(user_id: uuid.UUID | None):  # noqa: ANN202
    return (
        IdempotencyRecord.user_id.is_(None)
        if user_id is None
        else IdempotencyRecord.user_id == user_id
    )


async def _find_record(
    session: AsyncSession, *, key: str, method: str, path: str, user_id: uuid.UUID | None
) -> IdempotencyRecord | None:
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.key == key,
        IdempotencyRecord.method == method,
        IdempotencyRecord.path == path,
        _user_clause(user_id),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _delete_reservation(
    *, key: str, method: str, path: str, user_id: uuid.UUID | None
) -> None:
    """删除占位记录（请求 5xx / 未处理异常时调用），允许后续请求重新执行。"""
    async with async_session_factory() as session:
        record = await _find_record(session, key=key, method=method, path=path, user_id=user_id)
        if record is not None and record.response_status == RESERVED_STATUS:
            await session.delete(record)
            await session.commit()


async def idempotency_guard(
    request: Request,
) -> None:
    """命令接口幂等守卫依赖项。未携带 Idempotency-Key 时直接放行。

    查询一律使用独立短会话：等待重放的轮询若复用请求会话，ORM identity map
    会缓存占位记录（response_status=0）而看不到并发事务的更新。
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        return

    user_id = getattr(request.state, "user_id", None)
    method, path = request.method, request.url.path
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS

    while True:
        async with async_session_factory() as query_session:
            record = await _find_record(
                query_session, key=key, method=method, path=path, user_id=user_id
            )
        if record is not None and record.response_status != RESERVED_STATUS:
            # 已完成记录：直接重放首次响应
            raise IdempotentReplay(record.response_status, record.response_body)
        if record is None:
            # 无记录：尝试插入占位记录抢占执行权（独立会话立即提交；
            # 唯一索引兜底——并发下同键只有一个请求能占位成功）
            try:
                async with async_session_factory() as claim_session:
                    claim_session.add(
                        IdempotencyRecord(
                            key=key,
                            user_id=user_id,
                            method=method,
                            path=path,
                            response_status=RESERVED_STATUS,
                            response_body={},
                        )
                    )
                    await claim_session.commit()
            except IntegrityError:
                pass  # 并发请求抢先占位，转入等待重放
            else:
                request.state.idempotency_pending = {
                    "key": key,
                    "user_id": user_id,
                    "method": method,
                    "path": path,
                }
                return
        # 记录处于占位状态（另一并发请求正在执行）：等待其响应落库后重放
        if time.monotonic() > deadline:
            raise ApiException(
                409,
                ErrorCodes.IDEMPOTENCY_IN_PROGRESS,
                "相同幂等键的请求正在处理中，请稍后重试",
            )
        await asyncio.sleep(WAIT_INTERVAL_SECONDS)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """请求结束后把首次响应写回占位记录，供幂等重放。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # 未处理异常：释放占位，允许后续同键请求重新执行
            await self._release_if_pending(request)
            raise

        pending: dict[str, Any] | None = getattr(request.state, "idempotency_pending", None)
        if pending is None:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        headers = dict(response.headers)
        headers.pop("content-length", None)
        final = Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

        if response.status_code >= 500:
            # 5xx 不落库：删除占位，允许客户端重试真正执行
            await _delete_reservation(**pending)
            return final

        try:
            parsed: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"_raw": body.decode("utf-8", errors="replace")[:4096]}

        # 把首次响应写回占位记录（占位由 guard 先行插入，正常路径必存在）
        async with async_session_factory() as session:
            await session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == pending["key"],
                    IdempotencyRecord.method == pending["method"],
                    IdempotencyRecord.path == pending["path"],
                    _user_clause(pending["user_id"]),
                )
                .values(response_status=response.status_code, response_body=parsed)
            )
            await session.commit()
        return final

    async def _release_if_pending(self, request: Request) -> None:
        pending: dict[str, Any] | None = getattr(request.state, "idempotency_pending", None)
        if pending is not None:
            await _delete_reservation(**pending)
