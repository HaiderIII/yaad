"""Add Google OAuth support

Revision ID: c2a3b4d5e6f7
Revises: b51f48afdfff
Create Date: 2026-01-04 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2a3b4d5e6f7'
down_revision: Union[str, None] = 'b51f48afdfff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make github_id nullable (users can now login with Google only)
    op.alter_column('users', 'github_id',
                    existing_type=sa.Integer(),
                    nullable=True)

    # Add google_id column
    op.add_column('users',
                  sa.Column('google_id', sa.String(length=255), nullable=True))

    # Create unique index for google_id
    op.create_index(op.f('ix_users_google_id'), 'users', ['google_id'], unique=True)


def downgrade() -> None:
    # Drop google_id index and column
    op.drop_index(op.f('ix_users_google_id'), table_name='users')
    op.drop_column('users', 'google_id')

    # Make github_id required again (will fail if any null values exist)
    op.alter_column('users', 'github_id',
                    existing_type=sa.Integer(),
                    nullable=False)
