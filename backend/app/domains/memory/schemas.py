"""记忆检索接口的入参/出参 Schema（设计文档第 11 节）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS
from app.domains.memory.member_stats import MemberStats
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


# ---------- 团队记忆：成员统计（设计文档第 7 节①，M3.3） ----------


class MemberStatsOut(BaseModel):
    """成员完成数、负载与按时完成率（分配页面与 Agent 工具共用，M6.2）。

    on_time_rate 为 None 表示无已完成样本（前端展示"暂无数据"）；
    sample_sufficient=False 时前端标注"样本不足"（16.8）。
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


# ---------- 团队记忆：成员文字档案（设计文档第 7 节②，M3.5） ----------


class MemberProfileUpsertIn(BaseModel):
    """负责人创建/更新成员档案（写完直接生效，不走确认流程）。"""

    content: str = Field(min_length=1, max_length=4000)


class MemberProfileOut(BaseModel):
    """成员档案：随人走（users.id），项目内全员可读含本人（16.1）。

    membership_active：目标在当前项目的成员状态（16.7 停用标记）——
    True/False = 本项目在职/停用；null = 不是本项目成员。
    """

    user_id: uuid.UUID
    content: str
    created_by: MemberBrief
    last_edited_by: MemberBrief
    membership_active: bool | None
    created_at: datetime
    updated_at: datetime


# ---------- 知识库问答（设计文档第 11 节②，M7.3） ----------


class MemoryQaRequest(BaseModel):
    """一问一答入参（本期无多轮、无流式，第 11 节）。"""

    question: str = Field(min_length=1, max_length=2000)


class QaSourceOut(BaseModel):
    """依据/线索：来源定位 + 片段内容（点击可查看原文）。"""

    source_type: str
    source_id: uuid.UUID
    title: str
    snippet: str


class MemoryQaResponse(BaseModel):
    """answered 附依据列表；refused 附最接近的线索（16.13 宁拒答不编造）。"""

    status: str  # "answered" | "refused"
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
