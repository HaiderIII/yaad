"""Add streaming availability to recommendations.

Revision ID: f1g2h3i4j5k6
Revises: c3d4e5f6g7h8
Create Date: 2025-01-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f1g2h3i4j5k6"
down_revision = "b1b57cea3bca"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("is_streamable", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "recommendations",
        sa.Column("streaming_providers", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendations", "streaming_providers")
    op.drop_column("recommendations", "is_streamable")
