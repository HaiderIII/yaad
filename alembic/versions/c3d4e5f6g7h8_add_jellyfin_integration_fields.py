"""add_jellyfin_integration_fields

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6a7
Create Date: 2026-01-10 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6g7h8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def column_exists(table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(table_name: str, index_name: str) -> bool:
    """Check if an index exists on a table."""
    bind = op.get_bind()
    inspector = inspect(bind)
    indexes = [idx['name'] for idx in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    # Add Jellyfin integration fields to users table
    if not column_exists('users', 'jellyfin_url'):
        op.add_column('users', sa.Column('jellyfin_url', sa.String(500), nullable=True))
    if not column_exists('users', 'jellyfin_api_key'):
        op.add_column('users', sa.Column('jellyfin_api_key', sa.String(100), nullable=True))
    if not column_exists('users', 'jellyfin_user_id'):
        op.add_column('users', sa.Column('jellyfin_user_id', sa.String(100), nullable=True))
    if not column_exists('users', 'jellyfin_sync_enabled'):
        op.add_column('users', sa.Column('jellyfin_sync_enabled', sa.Boolean(), nullable=False, server_default='false'))

    # Add Jellyfin sync metadata to media table (if not already present)
    if not column_exists('media', 'jellyfin_id'):
        op.add_column('media', sa.Column('jellyfin_id', sa.String(100), nullable=True))
    if not column_exists('media', 'jellyfin_etag'):
        op.add_column('media', sa.Column('jellyfin_etag', sa.String(100), nullable=True))
    if not column_exists('media', 'last_jellyfin_sync'):
        op.add_column('media', sa.Column('last_jellyfin_sync', sa.DateTime(timezone=True), nullable=True))

    # Add index for Jellyfin ID lookup (if not already present)
    if not index_exists('media', 'ix_media_jellyfin_id'):
        op.create_index('ix_media_jellyfin_id', 'media', ['jellyfin_id'])


def downgrade() -> None:
    # Remove index
    op.drop_index('ix_media_jellyfin_id', table_name='media')

    # Remove media columns
    op.drop_column('media', 'last_jellyfin_sync')
    op.drop_column('media', 'jellyfin_etag')
    op.drop_column('media', 'jellyfin_id')

    # Remove users columns
    op.drop_column('users', 'jellyfin_sync_enabled')
    op.drop_column('users', 'jellyfin_user_id')
    op.drop_column('users', 'jellyfin_api_key')
    op.drop_column('users', 'jellyfin_url')
