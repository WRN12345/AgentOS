"""基于 `SSE` 的项目实时事件流。

浏览器 `EventSource` 不能自定义请求头，因此令牌和项目 ID 优先从查询参数读取，
并分别兼容 `Authorization` 与 `X-Project-Id` 请求头。连接只订阅当前成员的
`Redis Pub/Sub` 频道，并在流运行期间持续复验账号和成员状态。

`Last-Event-ID` 仅被接收而不会触发补发，因为 `Redis Pub/Sub` 不保存历史事件。
此通道只读，所有写操作仍通过可鉴权、可审计的 `REST` 接口完成。
"""

import json
import time
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.identity.security import decode_access_token
from app.domains.identity.service import get_user_by_id
from app.domains.identity.models import User
from app.domains.project.models import ProjectMember
from app.domains.project.service import get_member_by_user
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.events import channel_for

router = APIRouter(prefix="/events", tags=["events"])
logger = setup_logging("backend")

HEARTBEAT_SECONDS = 15.0
AUTH_RECHECK_SECONDS = 5.0


async def _resolve_member(
    request: Request, session: AsyncSession, token: str | None
) -> ProjectMember:
    """解析 `SSE` 连接的用户和项目成员身份。

    `token` 查询参数优先，`Authorization` 请求头作为回退；项目上下文优先取
    `project_id` 查询参数，再回退到 `X-Project-Id` 请求头。
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

    # `EventSource` 不能自定义请求头，因此优先使用查询参数
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
    """将事件编码为含 `id`、`event` 和 `data` 的 `SSE` 帧。"""
    data = json.dumps(payload, ensure_ascii=False)
    return (
        f"id: {payload.get('id', '')}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {data}\n\n"
    )


async def _stream_identity_is_active(member_id: uuid.UUID) -> bool:
    """复验账号与成员状态，使禁用或移除在已有连接上及时生效。"""
    async with async_session_factory() as session:
        member = await session.get(ProjectMember, member_id)
        if member is None or not member.is_active:
            return False
        user = await session.get(User, member.user_id)
        return user is not None and user.is_active


async def _event_generator(request: Request, member_id: uuid.UUID) -> AsyncGenerator[str, None]:
    redis_client = create_redis_client()
    pubsub = redis_client.pubsub()
    channel = channel_for(member_id)
    try:
        await pubsub.subscribe(channel)
        yield ": connected\n\n"
        last_ping = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            if not await _stream_identity_is_active(member_id):
                logger.info("event stream closed after identity revocation: member_id=%s", member_id)
                break
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=AUTH_RECHECK_SECONDS
            )
            if message is None:
                if time.monotonic() - last_ping >= HEARTBEAT_SECONDS:
                    yield ": ping\n\n"  # 心跳注释帧用于防止反向代理空闲超时
                    last_ping = time.monotonic()
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            if not await _stream_identity_is_active(member_id):
                break
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
    """订阅当前成员的只读实时事件流。"""
    # 认证使用短会话，流式响应期间不占用数据库连接
    async with async_session_factory() as session:
        member = await _resolve_member(request, session, token)

    # `Redis Pub/Sub` 无历史事件，因此接受 `Last-Event-ID` 但不补发
    _last_event_id = request.headers.get("Last-Event-ID")

    return StreamingResponse(
        _event_generator(request, member.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁止 `nginx` 等反向代理缓冲流式响应
        },
    )
