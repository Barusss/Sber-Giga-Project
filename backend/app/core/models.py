import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.types import Vector384


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
    extraction_version: Mapped[str] = mapped_column(String(32), default="ocr-v2", server_default="ocr-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    project: Mapped[Project] = relationship(back_populates="documents")
    fragments: Mapped[list["Fragment"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    facts: Mapped[list["Fact"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Fragment(Base):
    __tablename__ = "fragments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    block_type: Mapped[str] = mapped_column(String(32), default="paragraph", server_default="page")
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    bbox: Mapped[list | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    extraction_method: Mapped[str] = mapped_column(String(16), default="text", server_default="legacy")
    document: Mapped[Document] = relationship(back_populates="fragments")
    chunk_sources: Mapped[list["ChunkSource"]] = relationship(back_populates="fragment")
    fact_evidence: Mapped[list["FactEvidence"]] = relationship(back_populates="fragment")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "position", name="uq_chunks_document_position"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Vector384(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chunking_version: Mapped[str] = mapped_column(String(32), default="chunks-v1", server_default="chunks-v1")
    document: Mapped[Document] = relationship(back_populates="chunks")
    sources: Mapped[list["ChunkSource"]] = relationship(back_populates="chunk", cascade="all, delete-orphan", order_by="ChunkSource.position")


class ChunkSource(Base):
    __tablename__ = "chunk_sources"
    __table_args__ = (UniqueConstraint("chunk_id", "position", name="uq_chunk_sources_order"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), index=True)
    fragment_id: Mapped[str] = mapped_column(ForeignKey("fragments.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    chunk: Mapped[Chunk] = relationship(back_populates="sources")
    fragment: Mapped[Fragment] = relationship(back_populates="chunk_sources")


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (Index("ix_facts_document_type", "document_id", "fact_type"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    fact_type: Mapped[str] = mapped_column(String(96))
    value: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_version: Mapped[str] = mapped_column(String(64), default="manual", server_default="manual")
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    document: Mapped[Document] = relationship(back_populates="facts")
    evidence: Mapped[list["FactEvidence"]] = relationship(back_populates="fact", cascade="all, delete-orphan")


class FactEvidence(Base):
    __tablename__ = "fact_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fact_id: Mapped[str] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"), index=True)
    fragment_id: Mapped[str] = mapped_column(ForeignKey("fragments.id"), index=True)
    quote: Mapped[str] = mapped_column(Text)
    fact: Mapped[Fact] = relationship(back_populates="evidence")
    fragment: Mapped[Fragment] = relationship(back_populates="fact_evidence")


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
