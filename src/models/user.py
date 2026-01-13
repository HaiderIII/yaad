"""User model."""

from typing import TYPE_CHECKING

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.media import BookLocation, Media, Tag
    from src.models.recommendation import Recommendation


class User(Base, TimestampMixin):
    """User model for authentication and library ownership."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    locale: Mapped[str] = mapped_column(String(5), default="en")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)

    # Streaming preferences
    country: Mapped[str] = mapped_column(String(2), default="FR")  # ISO 3166-1 alpha-2
    streaming_platforms: Mapped[list] = mapped_column(JSON, default=list)  # List of provider IDs

    # External service connections
    letterboxd_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Kobo integration (stores device credentials for API access)
    kobo_user_key: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    kobo_device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Jellyfin integration
    jellyfin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    jellyfin_api_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    jellyfin_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    jellyfin_sync_enabled: Mapped[bool] = mapped_column(default=False)

    # YouTube Watch Later integration (OAuth2 with refresh token)
    youtube_refresh_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    youtube_playlist_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    youtube_sync_enabled: Mapped[bool] = mapped_column(default=False)

    # OAuth provider IDs (nullable - user has at least one)
    github_id: Mapped[int | None] = mapped_column(unique=True, index=True, nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)

    # Relationships
    # Using lazy="select" to prevent automatic loading of all media when fetching user
    # Media should be explicitly queried with filters/pagination
    media: Mapped[list["Media"]] = relationship(
        "Media",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )
    tags: Mapped[list["Tag"]] = relationship(
        "Tag",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )
    book_locations: Mapped[list["BookLocation"]] = relationship(
        "BookLocation",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        "Recommendation",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username})>"
