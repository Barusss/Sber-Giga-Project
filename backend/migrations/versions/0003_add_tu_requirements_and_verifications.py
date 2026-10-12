"""add_tu_requirements_and_verifications

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    requirement_category = sa.Enum(
        "power", "reliability", "voltage", "connection_point",
        "metering", "protection", "equipment", "other",
        name="requirementcategory"
    )
    requirement_status = sa.Enum(
        "fulfilled", "missing", "needs_review",
        name="requirementstatus"
    )

    op.create_table(
        "tu_requirements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("clause_number", sa.String(length=32), nullable=False),
        sa.Column("category", requirement_category, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tu_requirements_project_id", "tu_requirements", ["project_id"])
    op.create_index("ix_tu_requirements_document_id", "tu_requirements", ["document_id"])

    op.create_table(
        "requirement_verifications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), sa.ForeignKey("tu_requirements.id"), nullable=False),
        sa.Column("status", requirement_status, nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("matched_znp_clause_number", sa.String(length=32), nullable=True),
        sa.Column("matched_znp_text", sa.Text(), nullable=True),
        sa.Column("tu_quote", sa.Text(), nullable=True),
        sa.Column("znp_quote", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("is_confirmed_by_human", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("human_override_status", requirement_status, nullable=True),
        sa.Column("human_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requirement_verifications_project_id", "requirement_verifications", ["project_id"])
    op.create_index("ix_requirement_verifications_requirement_id", "requirement_verifications", ["requirement_id"])


def downgrade():
    op.drop_index("ix_requirement_verifications_requirement_id", table_name="requirement_verifications")
    op.drop_index("ix_requirement_verifications_project_id", table_name="requirement_verifications")
    op.drop_table("requirement_verifications")

    op.drop_index("ix_tu_requirements_document_id", table_name="tu_requirements")
    op.drop_index("ix_tu_requirements_project_id", table_name="tu_requirements")
    op.drop_table("tu_requirements")

    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TYPE IF EXISTS requirementcategory"))
        op.execute(sa.text("DROP TYPE IF EXISTS requirementstatus"))