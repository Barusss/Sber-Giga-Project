"""Track separate DOCX tables for unambiguous parent-clause scope."""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "fragments",
        sa.Column("container_id", sa.String(length=64), nullable=True),
    )


def downgrade():
    op.drop_column("fragments", "container_id")
