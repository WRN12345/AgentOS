"""记忆检索接口的入参/出参 Schema（设计文档第 11 节）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS
from app.domains.memory.retriever import RetrievalResult
from app.domains.memory.search import CALLER_LEADER_QUERY, CALLER_MEMBER_QA
from app.domains.work_items.schemas import MemberBrief

#: HTTP 路径允许的调用方标识（agent_assignment 仅供 Agent 内部调用，16.12）
_HTTP_CALLERS = (CALLER_MEMBER_QA, CALLER_LEADER_QUERY)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    source_types: list[str] | None = None
    limit: int | None = Field(default=None, ge=1, le=20)
    caller: str | None = Field(
        default=None,
        description=f"调用方标识，仅允许 {_HTTP_CALLERS}；缺省 member_qa",
    )


class MemorySearchHit(BaseModel):
    chunk_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    content: str
    distance: float


class MemorySearchResponse(BaseModel):
    results: list[MemorySearchHit]

    @classmethod
    def from_results(cls, results: list[RetrievalResult]) -> "MemorySearchResponse":
        return cls(
            results=[
                MemorySearchHit(
                    chunk_id=r.chunk_id,
                    source_type=r.source_type,
                    source_id=r.source_id,
                    content=r.content,
                    distance=r.distance,
                )
                for r in results
            ]
        )


# ---------- 核心记忆条目（设计文档第 8 节，M4.3） ----------


class CoreMemoryEntryCreateIn(BaseModel):
    """负责人手写条目（种子记忆，16.11）：立即生效。"""

    content: str = Field(min_length=1, max_length=CORE_MEMORY_BUDGET_CHARS)


class CoreMemoryEntryOut(BaseModel):
    """核心记忆条目：成员可见来源信息——谁提的、谁确认的、何时生效（第 8 节）。

    proposed_by 为 None 表示 Agent 提议（经负责人确认后生效，M4.4）。
    """

    id: uuid.UUID
    scope: str
    content: str
    status: str
    proposed_by: MemberBrief | None
    confirmed_by: MemberBrief
    effective_at: datetime
    created_at: datetime


class CoreMemoryEntryListOut(BaseModel):
    """条目列表 + 容量占用（供前端展示预算，第 8 节）。"""

    entries: list[CoreMemoryEntryOut]
    used_chars: int
    budget_chars: int
