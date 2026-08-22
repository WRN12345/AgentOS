"""记忆检索接口的入参/出参 Schema（设计文档第 11 节）。"""

import uuid

from pydantic import BaseModel, Field

from app.domains.memory.retriever import RetrievalResult
from app.domains.memory.search import CALLER_LEADER_QUERY, CALLER_MEMBER_QA

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
