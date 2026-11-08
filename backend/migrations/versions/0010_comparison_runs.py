"""Project comparison plans and preserved run snapshots.

Existing requirement_verifications continue to provide the current working
state; a separate run snapshot stores the actual result of each execution.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "comparison_pairs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tu_document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("znp_document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "tu_document_id", "znp_document_id", name="uq_comparison_project_pair"),
    )
    op.create_index("ix_comparison_pairs_project_id", "comparison_pairs", ["project_id"])
    op.create_index("ix_comparison_pairs_tu_document_id", "comparison_pairs", ["tu_document_id"])
    op.create_index("ix_comparison_pairs_znp_document_id", "comparison_pairs", ["znp_document_id"])
    op.create_table(
        "comparison_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("pair_id", sa.String(length=36), sa.ForeignKey("comparison_pairs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_comparison_runs_pair_id", "comparison_runs", ["pair_id"])


def downgrade():
    op.drop_index("ix_comparison_runs_pair_id", table_name="comparison_runs")
    op.drop_table("comparison_runs")
    op.drop_index("ix_comparison_pairs_znp_document_id", table_name="comparison_pairs")
    op.drop_index("ix_comparison_pairs_tu_document_id", table_name="comparison_pairs")
    op.drop_index("ix_comparison_pairs_project_id", table_name="comparison_pairs")
    op.drop_table("comparison_pairs")
