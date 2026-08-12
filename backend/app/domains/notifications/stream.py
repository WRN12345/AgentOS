"""SSE 实时事件流（4.3、12.6 节，T3.6）。

- `GET /events/stream`：`text/event-stream`，按成员专属 Redis 频道
  （infrastructure/events）向客户端下发实时事件；
- 认证：浏览器 EventSource 不能自定义请求头，支持 `?token=<access_token>`
  查询参数（同时兼容 `Authorization: Bearer` 头，便于 curl 调试）；
- 项目上下文：同样走 query `?project_id=<uuid>`（EventSource 不能带 header），
  并以 `X-Project-Id` 头兜底（便于 curl 调试）；连接只收当前项目事件（4.3 节）；
  校验规则与 get_current_member 一致（JWT 解析 → 用户有效且令牌版本匹配 →
  项目成员有效）；
- 帧格式：`id:` / `event:` / `data:` 三段；15s 无事件发 `: ping` 注释帧，
  防反向代理空闲超时；
- `Last-Event-ID`：仅接受、不补发（最小实现）——Redis Pub/Sub 无历史，
  重连期间漏发由前端"收到任意事件即失效相关查询缓存"兜底；
- 只读通道：所有写操作仍走可鉴权、可审计的 REST（4.3 节），
  本端点不含任何写路径，也不引入 WebSocket。
"""

import json
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.errors import ApiException, ErrorCodes
from app.domains.identity.security import decode_access_token
from app.domains.identity.service import get_user_by_id
from app.domains.project.models import ProjectMember
from app.domains.project.service import get_member_by_user
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.events import channel_for

router = APIRouter(prefix="/events", tags=["events"])

HEARTBEAT_SECONDS = 15.0


async def _resolve_member(
    request: Request, session: AsyncSession, token: str | None
) -> ProjectMember:
    """解析 SSE 连接的成员身份：`?token=` 优先，Authorization 头兜底。
    多项目后同时从 X-Project-Id 请求头读取项目上下文。
    """
    raw = token
    if not raw:
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "缺少访问令牌")

    payload = decode_access_token(raw)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期") from None

    user = await get_user_by_id(session, user_id)
    if user is None or user.token_version != payload.get("tv"):
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期")
    if not user.is_active:
        raise ApiException(403, ErrorCodes.USER_DISABLED, "账号已被禁用")

    # 多项目：优先 query 参数 project_id（浏览器 EventSource 不能自定义 header，
    # spec 4.3 要求走 query，与 token 同传法）；header X-Project-Id 兜底（便于 curl 调试）
    project_id_str = request.query_params.get("project_id") or request.headers.get(
        "X-Project-Id", ""
    )
    if not project_id_str:
        raise ApiException(400, ErrorCodes.MISSING_PROJECT_ID, "缺少项目上下文，请携带 X-Project-Id 请求头")
    try:
        project_id = uuid.UUID(project_id_str)
    except ValueError:
        raise ApiException(
            400, ErrorCodes.MISSING_PROJECT_ID, "X-Project-Id 格式无效，须为合法 UUID"
        ) from None

    member = await get_member_by_user(session, project_id, user.id)
    if member is None or not member.is_active:
        raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是项目成员或已被禁用")
    return member


def _format_sse(payload: dict) -> str:
    """SSE 帧：id:/event:/data: 三段，data 为完整事件载荷 JSON。"""
    data = json.dumps(payload, ensure_ascii=False)
    return (
        f"id: {payload.get('id', '')}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {data}\n\n"
    )


async def _event_generator(request: Request, member_id: uuid.UUID) -> AsyncGenerator[str, None]:
    redis_client = create_redis_client()
    pubsub = redis_client.pubsub()
    channel = channel_for(member_id)
    try:
        await pubsub.subscribe(channel)
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
            )
            if message is None:
                yield ": ping\n\n"  # 心跳注释帧：防反代空闲超时
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            yield _format_sse(payload)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await redis_client.aclose()


@router.get("/stream")
async def events_stream(
    request: Request,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    """订阅当前成员的实时事件流（只读）。"""
    # 认证只用一段短会话，流式期间不持有 DB 连接
    async with async_session_factory() as session:
        member = await _resolve_member(request, session, token)

    # Last-Event-ID 最小实现：接受头部但不补发（Pub/Sub 无历史），见模块 docstring
    _last_event_id = request.headers.get("Last-Event-ID")

    return StreamingResponse(
        _event_generator(request, member.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 告知 nginx 等反代不要缓冲该响应
        },
    )
