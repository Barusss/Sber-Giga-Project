"""Initial schema

Revision ID: 0001
Revises:
"""
from alembic import op
from app.core.db import Base
from app.core import models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    Base.metadata.create_all(bind=op.get_bind())

def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
