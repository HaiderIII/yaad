"""add_letterboxd_slug

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-09 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add letterboxd_slug column for films imported from Letterboxd
    op.add_column('media', sa.Column('letterboxd_slug', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('media', 'letterboxd_slug')
