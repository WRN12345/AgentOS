"""为命令接口提供基于 `Idempotency-Key` 的幂等控制。

接口通过 `Depends(idempotency_guard)` 启用幂等。守卫按项目、用户、请求方法、
路径和幂等键查找记录：已完成的请求直接重放首次响应；新请求通过插入占位记录
抢占执行权。数据库唯一约束保证并发请求中只有一个能够成功占位，其余请求等待
首次响应落库后再重放。

`IdempotencyMiddleware` 在请求成功后将状态码和响应体写回占位记录。请求失败或
抛出未处理异常时释放占位，使后续同键请求可以重新执行。

项目上下文取自 `X-Project-Id` 请求头。无项目或未登录的接口分别使用空项目、
空用户参与唯一性判断，因此同一幂等键可以在不同项目或用户下独立使用。接口同时
依赖身份认证时，必须先解析当前用户，再执行 `idempotency_guard`，以确保守卫能够
从 `request.state.user_id` 获取用户身份。
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
from starlette.responses import Response, StreamingResponse

from app.core.errors import ApiException, ErrorCodes, IdempotentReplay
from app.core.logging import setup_logging
from app.core.request_context import project_id_from_header
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.idempotency import IdempotencyRecord

logger = setup_logging("backend")

#: 占位状态表示执行权已被抢占，但首次响应尚未落库。
RESERVED_STATUS = 0
#: 并发请求等待首次响应的超时与轮询间隔，仅用于异常情况下兜底。
WAIT_TIMEOUT_SECONDS = 10.0
WAIT_INTERVAL_SECONDS = 0.05


def _user_clause(user_id: uuid.UUID | None):  # noqa: ANN202
    return (
        IdempotencyRecord.user_id.is_(None)
        if user_id is None
        else IdempotencyRecord.user_id == user_id
    )


def _project_clause(project_id: uuid.UUID | None):  # noqa: ANN202
    return (
        IdempotencyRecord.project_id.is_(None)
        if project_id is None
        else IdempotencyRecord.project_id == project_id
    )


async def _find_record(
    session: AsyncSession,
    *,
    key: str,
    method: str,
    path: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> IdempotencyRecord | None:
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.key == key,
        IdempotencyRecord.method == method,
        IdempotencyRecord.path == path,
        _project_clause(project_id),
        _user_clause(user_id),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _delete_reservation(
    *,
    key: str,
    method: str,
    path: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> None:
    """释放失败请求的占位记录，使后续同键请求可以重新执行。"""
    async with async_session_factory() as session:
        record = await _find_record(
            session, key=key, method=method, path=path, user_id=user_id, project_id=project_id
        )
        if record is not None and record.response_status == RESERVED_STATUS:
            await session.delete(record)
            await session.commit()


async def idempotency_guard(
    request: Request,
) -> None:
    """为命令接口抢占执行权，或重放同一幂等键的首次响应。

    未携带 `Idempotency-Key` 时直接放行。查询使用独立短会话，避免 ORM
    identity map 缓存占位记录，导致轮询无法观察到并发事务写入的响应。
    """
    key = request.headers.get("Idempotency-Key")
    if not key:
        return

    user_id = getattr(request.state, "user_id", None)
    project_id = project_id_from_header(request)
    method, path = request.method, request.url.path
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS

    while True:
        async with async_session_factory() as query_session:
            record = await _find_record(
                query_session,
                key=key,
                method=method,
                path=path,
                user_id=user_id,
                project_id=project_id,
            )
        if record is not None and record.response_status != RESERVED_STATUS:
            raise IdempotentReplay(record.response_status, record.response_body)
        if record is None:
            # 独立提交占位记录，让唯一索引决定并发请求中谁获得执行权。
            try:
                async with async_session_factory() as claim_session:
                    claim_session.add(
                        IdempotencyRecord(
                            key=key,
                            project_id=project_id,
                            user_id=user_id,
                            method=method,
                            path=path,
                            response_status=RESERVED_STATUS,
                            response_body={},
                        )
                    )
                    await claim_session.commit()
            except IntegrityError:
                pass  # 其他并发请求已占位，继续等待其响应。
            else:
                request.state.idempotency_pending = {
                    "key": key,
                    "project_id": project_id,
                    "user_id": user_id,
                    "method": method,
                    "path": path,
                }
                return
        # 占位记录尚未完成，等待执行方写入响应。
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
            # 异常请求不能占用幂等键，否则后续重试会一直等待。
            await self._release_if_pending(request)
            raise

        pending: dict[str, Any] | None = getattr(request.state, "idempotency_pending", None)
        if pending is None:
            return response

        body = b""
        if isinstance(response, StreamingResponse):
            async for chunk in response.body_iterator:
                body += chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        else:
            try:
                body = bytes(response.body)  # type: ignore[arg-type]
            except AttributeError:
                # 部分响应类型没有 `body` 属性，只能通过迭代器收集响应体。
                async for chunk in response.body_iterator:
                    body += chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        final = Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

        if not 200 <= response.status_code < 300:
            # 失败响应不参与重放，调用方修正请求后仍可使用同一幂等键重试。
            await _delete_reservation(**pending)
            return final

        try:
            parsed: Any = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"_raw": body.decode("utf-8", errors="replace")[:4096]}

        # 守卫已提交占位记录，此处只需写入首次成功响应。
        async with async_session_factory() as session:
            await session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == pending["key"],
                    IdempotencyRecord.method == pending["method"],
                    IdempotencyRecord.path == pending["path"],
                    _project_clause(pending["project_id"]),
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
