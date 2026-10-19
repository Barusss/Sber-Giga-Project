from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.core.models import DocumentKind, ReviewState, ReviewStatus

class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

class ProjectOut(Schema):
    id: str
    name: str
    state: ReviewState
    created_at: datetime

class FragmentOut(Schema):
    id: str
    document_id: str
    page: int
    position: int
    text: str
    block_type: str
    section_path: str | None
    bbox: list[float] | None
    extraction_method: str

class DocumentOut(Schema):
    id: str
    project_id: str
    kind: DocumentKind
    filename: str
    mime_type: str
    extraction_version: str
    created_at: datetime

class Evidence(BaseModel):
    document_id: str
    fragment_id: str | None = None
    page: int | None = None
    quote: str

class FindingCreate(BaseModel):
    module: Literal["normatives", "requirements"]
    type: str = Field(min_length=1, max_length=64)
    severity: Literal["info", "warning", "error"] = "warning"
    explanation: str = Field(min_length=1)
    suggested_action: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)

class FindingOut(Schema):
    id: str
    project_id: str
    module: str
    type: str
    severity: str
    explanation: str
    suggested_action: str | None
    evidence: list[Evidence]
    review_status: ReviewStatus
    created_at: datetime

class FindingReview(BaseModel):
    review_status: Literal["accepted", "rejected"]

class NormativeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1)
    status: Literal["active", "obsolete", "unknown"]
    replacement: str | None = None
    source_url: str | None = None

class NormativeUpdate(BaseModel):
    title: str | None = None
    status: Literal["active", "obsolete", "unknown"] | None = None
    replacement: str | None = None
    source_url: str | None = None

class NormativeOut(NormativeCreate, Schema):
    id: str
    updated_at: datetime | None = None

# The format is shared by the document, normative and requirement modules.
class ChunkSourceOut(BaseModel):
    fragment_id: str
    page: int
    start_offset: int
    end_offset: int
    bbox: list[float] | None = None


class ChunkOut(BaseModel):
    id: str
    document_id: str
    position: int
    page_start: int
    page_end: int
    section_path: str | None
    text: str
    embedding_model: str | None
    sources: list[ChunkSourceOut]


class FactEvidenceCreate(BaseModel):
    fragment_id: str
    quote: str = Field(min_length=1)


class FactCreate(BaseModel):
    fact_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,95}$")
    value: dict
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    extraction_version: str = Field(default="manual", min_length=1, max_length=64)
    evidence: list[FactEvidenceCreate] = Field(min_length=1)


class FactEvidenceOut(FactEvidenceCreate):
    document_id: str
    page: int


class FactOut(BaseModel):
    id: str
    document_id: str
    fact_type: str
    value: dict
    confidence: float | None
    extraction_version: str
    review_status: ReviewStatus
    created_at: datetime
    evidence: list[FactEvidenceOut]


class EmbeddingUpdate(BaseModel):
    embedding_model: str = Field(min_length=1, max_length=128)
    embedding: list[float] = Field(min_length=384, max_length=384)

    @field_validator("embedding")
    @classmethod
    def valid_embedding(cls, value):
        import math
        if not all(math.isfinite(x) for x in value) or not any(value):
            raise ValueError("Embedding must contain finite, nonzero values")
        return value


class SearchInput(BaseModel):
    query: str | None = Field(default=None, max_length=500)
    embedding_model: str | None = Field(default=None, max_length=128)
    embedding: list[float] | None = Field(default=None, min_length=384, max_length=384)
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_search(self):
        import math
        if not (self.query and self.query.strip()) and self.embedding is None:
            raise ValueError("Specify query or embedding")
        if self.embedding is not None:
            if not self.embedding_model:
                raise ValueError("embedding_model required with embedding")
            if not all(math.isfinite(x) for x in self.embedding) or not any(self.embedding):
                raise ValueError("Embedding must contain finite, nonzero values")
        return self


class SearchHit(BaseModel):
    chunk: ChunkOut
    score: float
    channels: list[str]
