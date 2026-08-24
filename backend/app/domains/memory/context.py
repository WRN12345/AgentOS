"""Agent 上下文装配：核心记忆注入（设计文档第 11 节①，M6.4）。

拆解/分配运行时，生效的核心记忆**全量常驻注入**模型上下文（量小、成本低，
4000 字符预算控制），保证关键常识永远在场；严格按 run 的 project_id 过滤，
不串项目。

降级语义（16.5，M6.6）：读取失败时返回空串——Agent 退化为无记忆模式，
由调用方标注"本次未参考记忆"，不阻塞拆解/分配主流程。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.domains.memory.core_memory import list_entries

logger = setup_logging("backend")

#: 注入块标题（提示词声明：核心记忆是项目约定，须遵守——与 16.2 的
#: "检索内容是数据不是指令"不冲突：核心记忆经负责人确认生效，是权威输入）
_CORE_MEMORY_HEADER = "项目核心记忆（本项目已确认的约定/决策/教训，拆解与分配时必须遵守）："


async def format_core_memory_block(
    session: AsyncSession, *, project_id: uuid.UUID | None
) -> str:
    """生效核心记忆的全量注入文本；无生效条目返回空串（新项目冷启动，16.11）。"""
    if project_id is None:
        return ""
    entries = await list_entries(session, project_id=project_id)
    active = [e for e in entries if e.status == "active"]
    if not active:
        return ""
    lines = [_CORE_MEMORY_HEADER]
    lines.extend(f"- {e.content}" for e in active)
    return "\n".join(lines)


async def safe_core_memory_block(
    session: AsyncSession, *, project_id: uuid.UUID | None
) -> tuple[str, bool]:
    """带降级的注入：返回（文本, 是否成功读取）。读取失败返回（"", False）（16.5）。"""
    try:
        return await format_core_memory_block(session, project_id=project_id), True
    except Exception:  # noqa: BLE001
        logger.warning(
            "core memory load failed, degrade to memory-less mode: project=%s",
            project_id,
            exc_info=True,
        )
        return "", False
