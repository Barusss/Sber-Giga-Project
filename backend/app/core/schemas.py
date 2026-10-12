from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
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

class DocumentOut(Schema):
    id: str
    project_id: str
    kind: DocumentKind
    filename: str
    mime_type: str
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

class NormativeOut(NormativeCreate, Schema):
    id: str
