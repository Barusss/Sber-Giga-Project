"""Traceable OCR fragments, search chunks and typed facts.

Revision ID: 0002
Revises: 0001
"""
from uuid import uuid4
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ENUM as PGEnum
from alembic import op

from app.core.types import Vector384

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("documents", sa.Column("extraction_version", sa.String(32), nullable=False, server_default="ocr-v1"))
    op.add_column("fragments", sa.Column("block_type", sa.String(32), nullable=False, server_default="page"))
    op.add_column("fragments", sa.Column("section_path", sa.Text(), nullable=True))
    op.add_column("fragments", sa.Column("bbox", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))
    op.add_column("fragments", sa.Column("extraction_method", sa.String(16), nullable=False, server_default="legacy"))
    # Older imports can contain multiple entries at the same page+position.
    # Keep old data intact: the constraint is only on new chunks.
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector384(), nullable=True),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("chunking_version", sa.String(32), nullable=False, server_default="chunks-v1"),
        sa.UniqueConstraint("document_id", "position", name="uq_chunks_document_position"),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_table(
        "chunk_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fragment_id", sa.String(36), sa.ForeignKey("fragments.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.UniqueConstraint("chunk_id", "position", name="uq_chunk_sources_order"),
    )
    op.create_index("ix_chunk_sources_chunk_id", "chunk_sources", ["chunk_id"])
    op.create_index("ix_chunk_sources_fragment_id", "chunk_sources", ["fragment_id"])
    op.create_table(
        "facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_type", sa.String(96), nullable=False),
        sa.Column("value", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("extraction_version", sa.String(64), nullable=False, server_default="manual"),
        sa.Column("review_status", sa.Enum("pending", "accepted", "rejected", name="reviewstatus").with_variant(
            PGEnum("pending", "accepted", "rejected", name="reviewstatus", create_type=False), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_facts_document_id", "facts", ["document_id"])
    op.create_index("ix_facts_document_type", "facts", ["document_id", "fact_type"])
    op.create_table(
        "fact_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fact_id", sa.String(36), sa.ForeignKey("facts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fragment_id", sa.String(36), sa.ForeignKey("fragments.id"), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
    )
    op.create_index("ix_fact_evidence_fact_id", "fact_evidence", ["fact_id"])
    op.create_index("ix_fact_evidence_fragment_id", "fact_evidence", ["fragment_id"])
    _backfill_legacy_fragments()
    if is_pg:
        op.execute("CREATE INDEX ix_chunks_text_fts ON chunks USING gin (to_tsvector('russian', text))")
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL")


def _backfill_legacy_fragments():
    """Index documents uploaded before the new OCR pipeline; do not re-OCR them."""
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, document_id, page, text FROM fragments ORDER BY document_id, page, position"
    ))
    positions: dict[str, int] = {}
    for fragment_id, document_id, page, content in rows:
        if not content:
            continue
        start = 0
        while start < len(content):
            end = min(start + 1400, len(content))
            if end < len(content):
                boundary = max(content.rfind("\n", start + 700, end),
                               content.rfind(" ", start + 700, end))
                if boundary > start:
                    end = boundary + 1
            chunk_id = str(uuid4())
            position = positions.get(document_id, 0)
            positions[document_id] = position + 1
            conn.execute(sa.text("""
                INSERT INTO chunks
                  (id, document_id, position, page_start, page_end, text, chunking_version)
                VALUES
                  (:id, :document_id, :position, :page_start, :page_end, :content, :version)
            """), dict(id=chunk_id, document_id=document_id, position=position,
                       page_start=page, page_end=page, content=content[start:end], version="legacy-v1"))
            conn.execute(sa.text("""
                INSERT INTO chunk_sources
                  (id, chunk_id, fragment_id, position, start_offset, end_offset)
                VALUES (:id, :chunk_id, :fragment_id, 0, :start_offset, :end_offset)
            """), dict(id=str(uuid4()), chunk_id=chunk_id, fragment_id=fragment_id,
                       start_offset=start, end_offset=end))
            start = end


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
        op.execute("DROP INDEX IF EXISTS ix_chunks_text_fts")
    for table in ("fact_evidence", "facts", "chunk_sources", "chunks"):
        op.drop_table(table)
    for col in ("extraction_method", "bbox", "section_path", "block_type"):
        op.drop_column("fragments", col)
    op.drop_column("documents", "extraction_version")
    # Keep vector extension: other applications/tables may use it.
