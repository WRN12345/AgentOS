import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    original_filename: str
    size_bytes: int
    created_at: datetime


class AnalysisIn(BaseModel):
    material_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: Literal["pending", "running", "succeeded", "failed"]
    error: str | None
    created_at: datetime


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True)


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20000)
    acceptance_criteria: str = Field(max_length=20000)
    clarification_questions: str = Field(max_length=10000)

    @field_validator("title", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Content must not be blank")
        return value.strip()


class EditIn(Content, VersionIn):
    pass


class ClarifyIn(VersionIn):
    note: str = Field(min_length=1, max_length=10000)

    @field_validator("note")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Note must not be blank")
        return value.strip()


class ReplyIn(ClarifyIn):
    content: Content | None = None


class DiscussionEntry(BaseModel):
    id: uuid.UUID
    author_id: uuid.UUID | None
    author_role: Literal["admin", "leader"]
    body: str
    created_at: datetime | None
    version: int | None


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: uuid.UUID
    filename: str
    chunk_id: str
    quote: str = Field(min_length=1, max_length=4000)


class Candidate(Content):
    source_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("acceptance_criteria", "clarification_questions", mode="before")
    @classmethod
    def text_items(cls, value: object) -> object:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return "\n".join(value)
        return value


class Candidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[Candidate] = Field(max_length=50)


class RequirementOut(Content):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sources: list[Source]
    status: Literal["draft", "confirmed", "excluded", "dispatched", "accepted", "clarification_requested"]
    version: int
    assignee_id: uuid.UUID | None
    leader_note: str | None
    discussion: list[DiscussionEntry]
    created_at: datetime
    updated_at: datetime
