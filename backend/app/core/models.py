import enum
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.db import Base

def now():
    return datetime.now(timezone.utc)

def new_id():
    return str(uuid4())

class DocumentKind(str, enum.Enum):
    tu = "tu"
    znp = "znp"
    other = "other"

class ReviewStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"

class ReviewState(str, enum.Enum):
    draft = "draft"
    approved = "approved"

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    state: Mapped[ReviewState] = mapped_column(Enum(ReviewState), default=ReviewState.draft)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    documents: Mapped[list["Document"]] = relationship(back_populates="project")

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[DocumentKind] = mapped_column(Enum(DocumentKind))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    project: Mapped[Project] = relationship(back_populates="documents")
    fragments: Mapped[list["Fragment"]] = relationship(back_populates="document", cascade="all, delete-orphan")

class Fragment(Base):
    __tablename__ = "fragments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    document: Mapped[Document] = relationship(back_populates="fragments")

class Normative(Base):
    __tablename__ = "normatives"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    replacement: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    module: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    explanation: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
