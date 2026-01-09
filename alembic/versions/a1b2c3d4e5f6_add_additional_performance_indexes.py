"""add_additional_performance_indexes

Revision ID: a1b2c3d4e5f6
Revises: 9d33dbf9223a
Create Date: 2026-01-09 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9d33dbf9223a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Index for sorted filtered results (catalogue with sorting)
    op.create_index(
        'ix_media_user_type_status_created',
        'media',
        ['user_id', 'type', 'status', 'created_at'],
        unique=False
    )

    # Index for rating-based sorting
    op.create_index(
        'ix_media_user_rating',
        'media',
        ['user_id', 'rating'],
        unique=False
    )

    # Index for finding stale streaming links
    op.create_index(
        'ix_media_user_streaming_updated',
        'media',
        ['user_id', 'streaming_links_updated'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_media_user_streaming_updated', table_name='media')
    op.drop_index('ix_media_user_rating', table_name='media')
    op.drop_index('ix_media_user_type_status_created', table_name='media')
