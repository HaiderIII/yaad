"""add genre_name to recommendations

Revision ID: 5863967cd92d
Revises: 4752856bc81c
Create Date: 2026-01-12 17:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5863967cd92d'
down_revision: Union[str, None] = '4752856bc81c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add genre_name column
    op.add_column('recommendations', sa.Column('genre_name', sa.String(100), nullable=True))

    # Create index for genre-based queries
    op.create_index('ix_recommendation_user_type_genre', 'recommendations', ['user_id', 'media_type', 'genre_name'])


def downgrade() -> None:
    op.drop_index('ix_recommendation_user_type_genre', table_name='recommendations')
    op.drop_column('recommendations', 'genre_name')
