"""Add fragment provenance to TU requirements and their verifications.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _schema_state() -> tuple[set[str], set[str], set[str], set[str]]:
    """Return columns, indexes and FK names for the affected tables.

    Databases deployed with the old ``0006`` revision already contain these
    columns while Alembic still reports ``0006``. The upgrade must tolerate
    that state instead of failing with DuplicateColumn.
    """
    inspector = sa.inspect(op.get_bind())
    tu_columns = {
        column["name"] for column in inspector.get_columns("tu_requirements")
    }
    verification_columns = {
        column["name"] for column in inspector.get_columns("requirement_verifications")
    }
    indexes = {
        index["name"] for index in inspector.get_indexes("tu_requirements")
    }
    foreign_keys: set[str] = set()
    if op.get_bind().dialect.name == "postgresql":
        foreign_keys = {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys("tu_requirements")
        }
    return tu_columns, verification_columns, indexes, foreign_keys


def upgrade():
    tu_columns, verification_columns, indexes, foreign_keys = _schema_state()

    if "fragment_id" not in tu_columns:
        op.add_column("tu_requirements", sa.Column(
            "fragment_id", sa.String(length=36), nullable=True,
        ))
    if "page" not in tu_columns:
        op.add_column("tu_requirements", sa.Column(
            "page", sa.Integer(), nullable=True,
        ))
    if "ix_tu_requirements_fragment_id" not in indexes:
        op.create_index(
            "ix_tu_requirements_fragment_id", "tu_requirements", ["fragment_id"],
        )

    if "matched_znp_fragment_id" not in verification_columns:
        op.add_column("requirement_verifications", sa.Column(
            "matched_znp_fragment_id", sa.String(length=36), nullable=True,
        ))
    if "matched_znp_page" not in verification_columns:
        op.add_column("requirement_verifications", sa.Column(
            "matched_znp_page", sa.Integer(), nullable=True,
        ))

    # SQLite cannot ADD CONSTRAINT in place; production PostgreSQL gets the FK.
    if (
        op.get_bind().dialect.name == "postgresql"
        and "fk_tu_requirements_fragment" not in foreign_keys
    ):
        op.create_foreign_key(
            "fk_tu_requirements_fragment", "tu_requirements",
            "fragments", ["fragment_id"], ["id"],
        )


def downgrade():
    tu_columns, verification_columns, indexes, foreign_keys = _schema_state()

    if (
        op.get_bind().dialect.name == "postgresql"
        and "fk_tu_requirements_fragment" in foreign_keys
    ):
        op.drop_constraint(
            "fk_tu_requirements_fragment", "tu_requirements", type_="foreignkey",
        )
    if "matched_znp_page" in verification_columns:
        op.drop_column("requirement_verifications", "matched_znp_page")
    if "matched_znp_fragment_id" in verification_columns:
        op.drop_column("requirement_verifications", "matched_znp_fragment_id")
    if "ix_tu_requirements_fragment_id" in indexes:
        op.drop_index("ix_tu_requirements_fragment_id", table_name="tu_requirements")
    if "page" in tu_columns:
        op.drop_column("tu_requirements", "page")
    if "fragment_id" in tu_columns:
        op.drop_column("tu_requirements", "fragment_id")
