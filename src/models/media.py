"""Media model and related entities."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.book import BookMetadata
    from src.models.film import FilmMetadata
    from src.models.user import User
    from src.models.youtube import YouTubeMetadata


class MediaType(str, enum.Enum):
    """Type of media."""

    FILM = "film"
    BOOK = "book"
    YOUTUBE = "youtube"
    PODCAST = "podcast"
    SHOW = "show"  # Spectacles vivants
    SERIES = "series"


class MediaStatus(str, enum.Enum):
    """Consumption status of media."""

    TO_CONSUME = "to_consume"
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class OwnershipType(str, enum.Enum):
    """Book ownership type."""

    NONE = "none"  # Don't own it
    PHYSICAL = "physical"  # Physical book
    EBOOK = "ebook"  # E-book
    BOTH = "both"  # Both physical and e-book


# Association tables
media_genres = Table(
    "media_genres",
    Base.metadata,
    Column("media_id", Integer, ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", Integer, ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

media_authors = Table(
    "media_authors",
    Base.metadata,
    Column("media_id", Integer, ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    Column("author_id", Integer, ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True),
)

media_tags = Table(
    "media_tags",
    Base.metadata,
    Column("media_id", Integer, ForeignKey("media.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Genre(Base):
    """Genre entity (shared across users, per media type)."""

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)

    __table_args__ = (UniqueConstraint("name", "media_type", name="uq_genre_name_type"),)

    def __repr__(self) -> str:
        return f"<Genre(id={self.id}, name={self.name})>"


class Author(Base):
    """Author/Director/Creator entity (shared across users, per media type)."""

    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (UniqueConstraint("name", "media_type", name="uq_author_name_type"),)

    def __repr__(self) -> str:
        return f"<Author(id={self.id}, name={self.name})>"


class Tag(Base, TimestampMixin):
    """User-defined tag (per user)."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="tags")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)

    def __repr__(self) -> str:
        return f"<Tag(id={self.id}, name={self.name})>"


class BookLocation(Base, TimestampMixin):
    """User-defined book storage location."""

    __tablename__ = "book_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g., "Salon", "Bureau", "Kindle"

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="book_locations")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_book_location_user_name"),)

    def __repr__(self) -> str:
        return f"<BookLocation(id={self.id}, name={self.name})>"


class Media(Base, TimestampMixin):
    """Base media entity with isolation per user."""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False, index=True)

    # Basic info
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # User tracking
    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.TO_CONSUME, nullable=False, index=True
    )
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Jellyfin integration (Phase 4)
    jellyfin_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    jellyfin_etag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_jellyfin_sync: Mapped[datetime | None] = mapped_column(nullable=True)

    # AI features (Phase 6)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Extended metadata for recommendations (TMDB data)
    tmdb_rating: Mapped[float | None] = mapped_column(Float, nullable=True)  # vote_average
    tmdb_vote_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)  # TMDB popularity score
    budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # Film budget in USD
    revenue: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # Film box office in USD
    original_language: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO 639-1
    production_countries: Mapped[list | None] = mapped_column(JSON, nullable=True)  # List of country codes
    cast: Mapped[list | None] = mapped_column(JSON, nullable=True)  # Top cast [{id, name, character, profile_path}]
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)  # TMDB keywords for themes
    collection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # TMDB collection/saga ID
    collection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g., "Marvel Cinematic Universe"
    certification: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Age rating (PG-13, R, etc.)
    tagline: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Film tagline

    # Series-specific extended fields
    number_of_seasons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number_of_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)  # User's watch progress
    series_status: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Returning Series, Ended, Canceled
    networks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # Broadcasting networks [{id, name, logo_path}]

    # Streaming deep links cache (from JustWatch)
    streaming_links: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {provider_id: {url, type}}
    streaming_links_updated: Mapped[datetime | None] = mapped_column(nullable=True, index=True)  # Last fetch time

    # Book ownership (for books only)
    ownership_type: Mapped[OwnershipType | None] = mapped_column(
        Enum(OwnershipType), default=None, nullable=True
    )  # Physical, e-book, both, or none
    ownership_location: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Where the physical book is stored

    # Letterboxd integration (for films imported from Letterboxd)
    letterboxd_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g., "avatar-fire-and-ash"

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="media")
    genres: Mapped[list[Genre]] = relationship("Genre", secondary=media_genres, lazy="selectin")
    authors: Mapped[list[Author]] = relationship("Author", secondary=media_authors, lazy="selectin")
    tags: Mapped[list[Tag]] = relationship("Tag", secondary=media_tags, lazy="selectin")

    # Type-specific metadata (one-to-one)
    film_metadata: Mapped["FilmMetadata | None"] = relationship(
        "FilmMetadata", back_populates="media", uselist=False, cascade="all, delete-orphan"
    )
    book_metadata: Mapped["BookMetadata | None"] = relationship(
        "BookMetadata", back_populates="media", uselist=False, cascade="all, delete-orphan"
    )
    youtube_metadata: Mapped["YouTubeMetadata | None"] = relationship(
        "YouTubeMetadata", back_populates="media", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "type", "external_id", name="uq_media_user_type_external"),
        # Composite indexes for common query patterns
        Index("ix_media_user_status", "user_id", "status"),  # Filter by user + status
        Index("ix_media_user_type", "user_id", "type"),  # Filter by user + type
        Index("ix_media_user_created", "user_id", "created_at"),  # Sort by created_at
        Index("ix_media_user_type_status", "user_id", "type", "status"),  # Filter by user + type + status
        # Additional indexes for optimized queries
        Index("ix_media_user_type_status_created", "user_id", "type", "status", "created_at"),  # Sorted filtered results
        Index("ix_media_user_rating", "user_id", "rating"),  # Rating-based sorting
        Index("ix_media_user_streaming_updated", "user_id", "streaming_links_updated"),  # Stale streaming links
    )

    def __repr__(self) -> str:
        return f"<Media(id={self.id}, title={self.title}, type={self.type})>"

    @property
    def is_complete(self) -> bool:
        """Check if media has all essential fields filled for its type."""
        # Common required fields for all types
        if not self.title or not self.cover_url:
            return False

        # Type-specific requirements
        if self.type == MediaType.FILM:
            return bool(self.year and self.duration_minutes and self.authors and self.description)
        elif self.type == MediaType.SERIES:
            return bool(self.year and self.authors and self.description)
        elif self.type == MediaType.BOOK:
            return bool(self.year and self.page_count and self.authors and self.description)
        elif self.type == MediaType.YOUTUBE:
            return bool(self.authors and self.external_url)  # Channel and URL
        elif self.type == MediaType.PODCAST:
            return bool(self.authors and self.description)  # Host and description
        elif self.type == MediaType.SHOW:
            return bool(self.year and self.description)

        return True

    @property
    def missing_fields(self) -> list[str]:
        """Get list of missing essential fields for this media type."""
        missing = []

        # Common fields
        if not self.title:
            missing.append("title")
        if not self.cover_url:
            missing.append("cover")

        # Type-specific fields
        if self.type == MediaType.FILM:
            if not self.year:
                missing.append("year")
            if not self.duration_minutes:
                missing.append("duration")
            if not self.authors:
                missing.append("director")
            if not self.description:
                missing.append("description")
        elif self.type == MediaType.SERIES:
            if not self.year:
                missing.append("year")
            if not self.authors:
                missing.append("creator")
            if not self.description:
                missing.append("description")
        elif self.type == MediaType.BOOK:
            if not self.year:
                missing.append("year")
            if not self.page_count:
                missing.append("pages")
            if not self.authors:
                missing.append("author")
            if not self.description:
                missing.append("description")
        elif self.type == MediaType.YOUTUBE:
            if not self.authors:
                missing.append("channel")
            if not self.external_url:
                missing.append("url")
        elif self.type == MediaType.PODCAST:
            if not self.authors:
                missing.append("host")
            if not self.description:
                missing.append("description")
        elif self.type == MediaType.SHOW:
            if not self.year:
                missing.append("year")
            if not self.description:
                missing.append("description")

        return missing
