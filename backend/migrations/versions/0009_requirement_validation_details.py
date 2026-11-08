"""Store versioned details of requirement verification."""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("requirement_verifications", sa.Column("validation_details", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("requirement_verifications", "validation_details")
