"""Initial schema (frozen for reliable upgrades from an empty DB).

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("state", sa.Enum("draft", "approved", name="reviewstate"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.Enum("tu", "znp", "other", name="documentkind"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_project_id", "documents", ["project_id"])
    op.create_table(
        "fragments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ix_fragments_document_id", "fragments", ["document_id"])
    op.create_table(
        "normatives",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("replacement", sa.String(128), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
    )
    op.create_index("ix_normatives_code", "normatives", ["code"], unique=True)
    op.create_table(
        "findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.Enum("pending", "accepted", "rejected", name="reviewstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_findings_project_id", "findings", ["project_id"])


def downgrade():
    for table in ("findings", "normatives", "fragments", "documents"):
        op.drop_table(table)
    op.drop_table("projects")
    if op.get_bind().dialect.name == "postgresql":
        for name in ("reviewstatus", "documentkind", "reviewstate"):
            op.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))
