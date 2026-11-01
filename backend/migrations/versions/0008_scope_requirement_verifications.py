"""Bind decisions to an immutable uploaded ZnP document, not just a TU requirement."""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("requirement_analyses",
        sa.Column("tu_document_id", sa.String(36), sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("znp_document_id", sa.String(36), sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("requirement_ids", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("documents", sa.Column("normative_extraction_completed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("requirement_verifications", sa.Column("method", sa.String(32), nullable=True))
    op.add_column("requirement_verifications", sa.Column("znp_document_id", sa.String(36), nullable=True))
    op.create_index("ix_requirement_verifications_znp_document_id", "requirement_verifications", ["znp_document_id"])
    # Only source-fragment provenance is sufficient to bind old decisions.
    # Unmatched legacy decisions stay unscoped and must be checked again.
    op.execute(sa.text("""
        UPDATE requirement_verifications SET znp_document_id = (
            SELECT fragments.document_id FROM fragments
            JOIN documents ON documents.id = fragments.document_id
            WHERE fragments.id = requirement_verifications.matched_znp_fragment_id
              AND documents.project_id = requirement_verifications.project_id
              AND documents.kind = 'znp'
        )
    """))
    with op.batch_alter_table("requirement_verifications") as batch:
        batch.create_foreign_key("fk_requirement_verification_znp", "documents", ["znp_document_id"], ["id"])


def downgrade():
    op.drop_table("requirement_analyses")
    op.drop_column("documents", "normative_extraction_completed")
    with op.batch_alter_table("requirement_verifications") as batch:
        batch.drop_constraint("fk_requirement_verification_znp", type_="foreignkey")
        batch.drop_index("ix_requirement_verifications_znp_document_id")
        batch.drop_column("znp_document_id")
        batch.drop_column("method")
