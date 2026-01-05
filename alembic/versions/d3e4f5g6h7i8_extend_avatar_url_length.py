"""Extend avatar_url length for Google avatars

Revision ID: d3e4f5g6h7i8
Revises: c2a3b4d5e6f7
Create Date: 2026-01-04 15:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e4f5g6h7i8'
down_revision: Union[str, None] = 'c2a3b4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Google avatar URLs can be very long (1000+ chars)
    op.alter_column('users', 'avatar_url',
                    existing_type=sa.String(length=500),
                    type_=sa.String(length=2000),
                    existing_nullable=True)


def downgrade() -> None:
    op.alter_column('users', 'avatar_url',
                    existing_type=sa.String(length=2000),
                    type_=sa.String(length=500),
                    existing_nullable=True)
