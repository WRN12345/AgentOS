"""记忆检索、核心记忆、成员档案和问答接口模型。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS
from app.domains.memory.member_stats import MemberStats
from app.domains.memory.retriever import RetrievalResult
from app.domains.memory.search import CALLER_LEADER_QUERY, CALLER_MEMBER_QA
from app.domains.work_items.schemas import MemberBrief

# `HTTP` 路径不能冒充仅供内部 `Agent` 使用的 `agent_assignment`
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


class CoreMemoryEntryCreateIn(BaseModel):
    """负责人手写并立即生效的核心记忆条目。"""

    content: str = Field(min_length=1, max_length=CORE_MEMORY_BUDGET_CHARS)


class CoreMemoryEntryOut(BaseModel):
    """包含提议者、确认者和生效时间的核心记忆条目。

    `proposed_by` 为空表示由 `Agent` 提议并经负责人确认。
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
    """核心记忆条目列表及容量占用。"""

    entries: list[CoreMemoryEntryOut]
    used_chars: int
    budget_chars: int


class MemberStatsOut(BaseModel):
    """分配页面和 `Agent` 共用的成员完成数、负载与按时完成率。

    `on_time_rate` 为空表示没有已完成样本；`sample_sufficient=False` 表示样本不足。
    """

    member_id: uuid.UUID
    display_name: str
    is_active: bool
    completed_total: int
    active_now: int
    completed_recent: int
    on_time_completed: int
    on_time_rate: float | None
    sample_sufficient: bool

    @classmethod
    def from_stats(cls, s: "MemberStats") -> "MemberStatsOut":
        return cls(
            member_id=s.member_id,
            display_name=s.display_name,
            is_active=s.is_active,
            completed_total=s.completed_total,
            active_now=s.active_now,
            completed_recent=s.completed_recent,
            on_time_completed=s.on_time_completed,
            on_time_rate=s.on_time_rate,
            sample_sufficient=s.sample_sufficient,
        )


class MemberProfileUpsertIn(BaseModel):
    """负责人创建/更新成员档案（写完直接生效，不走确认流程）。"""

    content: str = Field(min_length=1, max_length=4000)


class MemberProfileOut(BaseModel):
    """以 `users.id` 归属、可跨项目读取的成员档案。

    `membership_active` 表示目标用户在当前项目的成员状态；`None` 表示不属于该项目。
    """

    user_id: uuid.UUID
    content: str
    created_by: MemberBrief
    last_edited_by: MemberBrief
    membership_active: bool | None
    created_at: datetime
    updated_at: datetime


class MemoryQaRequest(BaseModel):
    """单轮、非流式知识库问答请求。"""

    question: str = Field(min_length=1, max_length=2000)


class QaSourceOut(BaseModel):
    """问答依据或拒答线索的来源定位和片段内容。

    `history_kind` 区分 `work_item` 与 `agent_run`，供前端决定是否显示工作项跳转；
    非 `history` 来源为空。
    """

    source_type: str
    source_id: uuid.UUID
    title: str
    snippet: str
    history_kind: str | None = None


class MemoryQaResponse(BaseModel):
    """`answered` 附依据列表，`refused` 附最接近的线索。"""

    status: str
    answer: str | None
    sources: list[QaSourceOut]
    clues: list[QaSourceOut]


class QaHistoryOut(BaseModel):
    """问答历史条目（仅本人可见）：问题 + 结论 + 依据/线索快照。"""

    id: uuid.UUID
    question: str
    status: str
    answer: str | None
    sources: list[QaSourceOut]
    created_at: datetime
