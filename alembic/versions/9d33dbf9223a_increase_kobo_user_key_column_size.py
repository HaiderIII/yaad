"""increase kobo_user_key column size

Revision ID: 9d33dbf9223a
Revises: b94c8bb0f23f
Create Date: 2026-01-09 08:54:40.385995
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d33dbf9223a'
down_revision: Union[str, None] = 'b94c8bb0f23f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Increase kobo_user_key from 500 to 2000 chars to accommodate tokens
    op.alter_column(
        'users',
        'kobo_user_key',
        type_=sa.String(length=2000),
        existing_type=sa.String(length=500),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'users',
        'kobo_user_key',
        type_=sa.String(length=500),
        existing_type=sa.String(length=2000),
        existing_nullable=True,
    )
