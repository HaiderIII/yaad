"""add_compound_indexes_for_performance

Revision ID: e99ef9741126
Revises: 0358b0c32116
Create Date: 2026-01-05 00:19:08.385767
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e99ef9741126'
down_revision: Union[str, None] = '0358b0c32116'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Compound indexes for common query patterns
    # These significantly improve performance for filtered/sorted queries

    # Index for dashboard "unfinished" queries: WHERE user_id = ? AND status IN (...)
    op.create_index(
        'ix_media_user_status',
        'media',
        ['user_id', 'status'],
        unique=False
    )

    # Index for "recent media" sorting: WHERE user_id = ? ORDER BY created_at DESC
    op.create_index(
        'ix_media_user_created',
        'media',
        ['user_id', 'created_at'],
        unique=False
    )

    # Index for catalogue with type filter: WHERE user_id = ? AND type = ? AND status = ?
    op.create_index(
        'ix_media_user_type_status',
        'media',
        ['user_id', 'type', 'status'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_media_user_type_status', table_name='media')
    op.drop_index('ix_media_user_created', table_name='media')
    op.drop_index('ix_media_user_status', table_name='media')
