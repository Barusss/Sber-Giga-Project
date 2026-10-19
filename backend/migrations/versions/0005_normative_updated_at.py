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
    # Nullable first for compatibility with populated PostgreSQL and SQLite tables.
    op.add_column("normatives", sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.execute(sa.text(
        "UPDATE normatives SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))


def downgrade():
    op.drop_column("normatives", "updated_at")
