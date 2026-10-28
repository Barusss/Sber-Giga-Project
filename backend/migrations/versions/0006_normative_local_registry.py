"""Track the official provenance of local and per-document normative statuses.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("normatives", sa.Column("registry_status", sa.String(length=128), nullable=True))
    op.add_column("normatives", sa.Column(
        "checked_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.add_column("normative_verifications", sa.Column("source", sa.String(length=32), nullable=True))

    # Do not infer a confirmation date for pre-existing registry rows: an
    # updated_at value may come from a manual edit. The date is filled only by
    # a confirmed official lookup or a forced refresh from now on.
    op.execute(sa.text(
        "UPDATE normative_verifications SET source = 'rosstandart' "
        "WHERE source IS NULL AND checked_at IS NOT NULL"
    ))


def downgrade():
    op.drop_column("normative_verifications", "source")
    op.drop_column("normatives", "checked_at")
    op.drop_column("normatives", "registry_status")
