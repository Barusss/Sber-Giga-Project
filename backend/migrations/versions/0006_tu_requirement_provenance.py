"""Add fragment provenance to TU requirements and their verifications.

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
    op.add_column("tu_requirements", sa.Column(
        "fragment_id", sa.String(length=36), nullable=True,
    ))
    op.add_column("tu_requirements", sa.Column(
        "page", sa.Integer(), nullable=True,
    ))
    op.create_index(
        "ix_tu_requirements_fragment_id", "tu_requirements", ["fragment_id"],
    )

    op.add_column("requirement_verifications", sa.Column(
        "matched_znp_fragment_id", sa.String(length=36), nullable=True,
    ))
    op.add_column("requirement_verifications", sa.Column(
        "matched_znp_page", sa.Integer(), nullable=True,
    ))

    # SQLite cannot ADD CONSTRAINT in place; production PostgreSQL gets the FK.
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_tu_requirements_fragment", "tu_requirements",
            "fragments", ["fragment_id"], ["id"],
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_tu_requirements_fragment", "tu_requirements", type_="foreignkey",
        )
    op.drop_column("requirement_verifications", "matched_znp_page")
    op.drop_column("requirement_verifications", "matched_znp_fragment_id")
    op.drop_index("ix_tu_requirements_fragment_id", table_name="tu_requirements")
    op.drop_column("tu_requirements", "page")
    op.drop_column("tu_requirements", "fragment_id")
