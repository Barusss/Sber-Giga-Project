"""Store last official verification for durable expert normative reports.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "normative_verifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("reference_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("registry_status", sa.String(128), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("replacement", sa.String(128), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("document_id", "code",
                            name="uq_normative_verifications_document_code"),
    )
    op.create_index("ix_normative_verifications_document_id",
                    "normative_verifications", ["document_id"])


def downgrade():
    op.drop_index("ix_normative_verifications_document_id", table_name="normative_verifications")
    op.drop_table("normative_verifications")
