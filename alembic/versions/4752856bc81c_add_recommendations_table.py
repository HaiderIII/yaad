"""add recommendations table

Revision ID: 4752856bc81c
Revises: 48b9f9ffge56
Create Date: 2026-01-12 15:45:05.434634
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4752856bc81c'
down_revision: Union[str, None] = '48b9f9ffge56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create recommendations table using existing mediatype enum
    op.execute("""
        CREATE TABLE recommendations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            media_type mediatype NOT NULL,
            external_id VARCHAR(100) NOT NULL,
            title VARCHAR(500) NOT NULL,
            year INTEGER,
            cover_url VARCHAR(500),
            description TEXT,
            score FLOAT NOT NULL,
            source VARCHAR(50) NOT NULL,
            external_url VARCHAR(500),
            extra_data JSON,
            is_dismissed BOOLEAN NOT NULL DEFAULT FALSE,
            added_to_library BOOLEAN NOT NULL DEFAULT FALSE,
            generated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_recommendation_user_type_external UNIQUE (user_id, media_type, external_id)
        )
    """)

    # Create indexes
    op.create_index('ix_recommendations_user_id', 'recommendations', ['user_id'])
    op.create_index('ix_recommendation_user_dismissed', 'recommendations', ['user_id', 'is_dismissed'])
    op.create_index('ix_recommendation_user_type_score', 'recommendations', ['user_id', 'media_type', 'score'])


def downgrade() -> None:
    op.drop_index('ix_recommendation_user_type_score', table_name='recommendations')
    op.drop_index('ix_recommendation_user_dismissed', table_name='recommendations')
    op.drop_index('ix_recommendations_user_id', table_name='recommendations')
    op.drop_table('recommendations')
