"""Recommendation model for storing AI-generated recommendations."""

from datetime import datetime

from sqlalchemy import JSON, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin
from src.models.media import MediaType


class Recommendation(Base, TimestampMixin):
    """Stored recommendation for a user.

    Recommendations can be:
    - Internal: Based on media already in the user's library (external_id references existing media)
    - External: New media discovered from APIs (TMDB, Open Library, etc.)
    """

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)

    # Media identification
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)  # TMDB ID, ISBN, YouTube ID, etc.
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Recommendation metadata
    score: Mapped[float] = mapped_column(Float, nullable=False)  # Confidence score 0-1
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # 'content', 'collaborative', 'hybrid', 'trending'
    genre_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Genre category for the recommendation

    # For external recommendations: additional info
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # JSON with additional metadata

    # External ratings (for display on tiles)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)  # TMDB rating 0-10

    # Streaming availability
    is_streamable: Mapped[bool] = mapped_column(default=False)  # Available on streaming platforms
    streaming_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)  # List of provider names

    # Tracking
    is_dismissed: Mapped[bool] = mapped_column(default=False)  # User dismissed this recommendation
    added_to_library: Mapped[bool] = mapped_column(default=False)  # User added to their library
    generated_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="recommendations")

    __table_args__ = (
        UniqueConstraint("user_id", "media_type", "external_id", name="uq_recommendation_user_type_external"),
        Index("ix_recommendation_user_type_score", "user_id", "media_type", "score"),
        Index("ix_recommendation_user_dismissed", "user_id", "is_dismissed"),
        Index("ix_recommendation_user_type_genre", "user_id", "media_type", "genre_name"),
    )

    def __repr__(self) -> str:
        return f"<Recommendation(id={self.id}, title={self.title}, score={self.score:.2f})>"
