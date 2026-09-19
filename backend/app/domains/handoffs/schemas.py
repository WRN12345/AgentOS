import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domains.deliverables.schemas import DeliverableOut
from app.domains.work_items.schemas import MemberBrief


class HandoffCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    target_work_item_id: uuid.UUID
    target_version: int = Field(ge=1)
    deliverable_id: uuid.UUID
    note: str | None = Field(default=None, max_length=10000)


class HandoffResponseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=10000)


class HandoffWorkItemBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: str
    version: int


class HandoffTargetOut(HandoffWorkItemBrief):
    assignee: MemberBrief


class HandoffOut(BaseModel):
    id: uuid.UUID
    source_work_item: HandoffWorkItemBrief
    target_work_item: HandoffWorkItemBrief
    deliverable: DeliverableOut
    sender: MemberBrief
    recipient: MemberBrief
    status: Literal["pending", "accepted", "changes_requested"]
    note: str | None
    response_note: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    responded_at: datetime | None
