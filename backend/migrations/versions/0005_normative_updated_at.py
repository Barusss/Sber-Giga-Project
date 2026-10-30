"""Add modification timestamps to the existing local normative registry.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Add nullable first so populated tables accept the new column, then
    # backfill before enforcing the NOT NULL contract used by the ORM model.
    op.add_column("normatives", sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.execute(sa.text(
        "UPDATE normatives SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))
    with op.batch_alter_table("normatives") as batch:
        batch.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False,
        )


def downgrade():
    op.drop_column("normatives", "updated_at")
