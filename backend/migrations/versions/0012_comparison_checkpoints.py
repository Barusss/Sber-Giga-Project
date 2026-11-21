"""Persist every LLM result and the pause flag for comparison workers."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operation_jobs", sa.Column(
        "pause_requested", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    # SQLite cannot ALTER table constraints in-place; batch mode rebuilds it.
    with op.batch_alter_table("comparison_runs") as batch:
        batch.add_column(sa.Column("operation_job_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_comparison_run_operation_job", "operation_jobs",
            ["operation_job_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_comparison_run_job_pair", ["operation_job_id", "pair_id"],
        )
        batch.create_index("ix_comparison_runs_operation_job_id", ["operation_job_id"])
    op.create_table(
        "comparison_llm_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36),
                  sa.ForeignKey("operation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "fingerprint",
                            name="uq_comparison_llm_checkpoint"),
    )
    op.create_index(
        "ix_comparison_llm_checkpoints_job_id",
        "comparison_llm_checkpoints", ["job_id"],
    )
    op.create_table(
        "comparison_atom_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36),
                  sa.ForeignKey("comparison_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(36),
                  sa.ForeignKey("operation_jobs.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("atom_id", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_comparison_atom_reviews_run_id", "comparison_atom_reviews", ["run_id"],
    )
    op.create_index(
        "ix_comparison_atom_reviews_atom_id", "comparison_atom_reviews", ["atom_id"],
    )


def downgrade():
    op.drop_index("ix_comparison_atom_reviews_atom_id",
                  table_name="comparison_atom_reviews")
    op.drop_index("ix_comparison_atom_reviews_run_id",
                  table_name="comparison_atom_reviews")
    op.drop_table("comparison_atom_reviews")
    op.drop_index("ix_comparison_llm_checkpoints_job_id",
                  table_name="comparison_llm_checkpoints")
    op.drop_table("comparison_llm_checkpoints")
    with op.batch_alter_table("comparison_runs") as batch:
        batch.drop_index("ix_comparison_runs_operation_job_id")
        batch.drop_constraint("uq_comparison_run_job_pair", type_="unique")
        batch.drop_constraint("fk_comparison_run_operation_job", type_="foreignkey")
        batch.drop_column("operation_job_id")
    op.drop_column("operation_jobs", "pause_requested")
