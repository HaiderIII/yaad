"""add_youtube_playlist_item_id

Revision ID: 48b9f9ffge56
Revises: 37a8f8eefd45
Create Date: 2026-01-10 15:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '48b9f9ffge56'
down_revision: Union[str, None] = '37a8f8eefd45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('youtube_metadata', sa.Column('playlist_item_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('youtube_metadata', 'playlist_item_id')
