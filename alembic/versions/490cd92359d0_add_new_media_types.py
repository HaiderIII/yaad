"""add_new_media_types

Revision ID: 490cd92359d0
Revises: d3e4f5g6h7i8
Create Date: 2026-01-04 17:04:03.346700
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '490cd92359d0'
down_revision: Union[str, None] = 'd3e4f5g6h7i8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new values to mediatype enum
    op.execute("ALTER TYPE mediatype ADD VALUE IF NOT EXISTS 'PODCAST'")
    op.execute("ALTER TYPE mediatype ADD VALUE IF NOT EXISTS 'SHOW'")
    op.execute("ALTER TYPE mediatype ADD VALUE IF NOT EXISTS 'SERIES'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values easily
    # Would need to recreate the enum type and update all references
    pass
