"""Pydantic schemas for API validation and serialization."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Re-export enums from media model (avoid duplication)
from src.models.media import MediaStatus as MediaStatusEnum
from src.models.media import MediaType as MediaTypeEnum
from src.models.media import OwnershipType as OwnershipTypeEnum


# Base schemas
class UserBase(BaseModel):
    """Base user schema."""

    username: str
    email: str | None = None
    avatar_url: str | None = None
    locale: str = "en"


class UserRead(UserBase):
    """User read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    github_id: int | None = None
    google_id: str | None = None
    created_at: datetime


class UserUpdate(BaseModel):
    """User update schema."""

    locale: str | None = None
    settings: dict | None = None
    country: str | None = None
    streaming_platforms: list[int] | None = None
    letterboxd_username: str | None = None


# Genre schemas
class GenreBase(BaseModel):
    """Base genre schema."""

    name: str
    media_type: MediaTypeEnum


class GenreRead(GenreBase):
    """Genre read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int


# Author schemas
class AuthorBase(BaseModel):
    """Base author schema."""

    name: str
    media_type: MediaTypeEnum


class AuthorRead(AuthorBase):
    """Author read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str | None = None


# Tag schemas
class TagBase(BaseModel):
    """Base tag schema."""

    name: str


class TagCreate(TagBase):
    """Tag creation schema."""

    pass


class TagRead(TagBase):
    """Tag read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# Book metadata schemas
class BookMetadataRead(BaseModel):
    """Book metadata read schema."""

    model_config = ConfigDict(from_attributes=True)

    kobo_id: str | None = None
    progress_percent: float | None = None
    isbn: str | None = None
    publisher: str | None = None


# Book Location schemas
class BookLocationBase(BaseModel):
    """Base book location schema."""

    name: str


class BookLocationCreate(BookLocationBase):
    """Book location creation schema."""

    pass


class BookLocationRead(BookLocationBase):
    """Book location read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# Media schemas
class MediaBase(BaseModel):
    """Base media schema."""

    title: str  # Original title (for films/series) or main title
    local_title: str | None = None  # French/local title if different from original
    type: MediaTypeEnum
    external_id: str | None = None
    year: int | None = None
    duration_minutes: int | None = None
    page_count: int | None = None
    description: str | None = None
    cover_url: str | None = None
    external_url: str | None = None
    status: MediaStatusEnum = MediaStatusEnum.TO_CONSUME
    rating: float | None = Field(None, ge=0.5, le=5)
    notes: str | None = None


class MediaCreate(MediaBase):
    """Media creation schema."""

    genre_ids: list[int] = []
    author_ids: list[int] = []
    tag_ids: list[int] = []
    consumed_at: datetime | None = None

    # Extended metadata (TMDB)
    tmdb_rating: float | None = None
    tmdb_vote_count: int | None = None
    popularity: float | None = None
    budget: int | None = None
    revenue: int | None = None
    original_language: str | None = None
    production_countries: list[str] | None = None
    cast: list[dict] | None = None
    keywords: list[str] | None = None
    collection_id: int | None = None
    collection_name: str | None = None
    certification: str | None = None
    tagline: str | None = None

    # Series-specific
    number_of_seasons: int | None = None
    number_of_episodes: int | None = None
    current_episode: int | None = None
    series_status: str | None = None
    networks: list[dict] | None = None

    # Letterboxd integration
    letterboxd_slug: str | None = None


class MediaUpdate(BaseModel):
    """Media update schema."""

    title: str | None = None
    local_title: str | None = None
    year: int | None = None
    duration_minutes: int | None = None
    page_count: int | None = None
    description: str | None = None
    cover_url: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    status: MediaStatusEnum | None = None
    rating: float | None = Field(None, ge=0.5, le=5)
    notes: str | None = None
    genre_ids: list[int] | None = None
    author_ids: list[int] | None = None
    tag_ids: list[int] | None = None
    current_episode: int | None = None  # Series watch progress
    # Book ownership (for books only)
    ownership_type: OwnershipTypeEnum | None = None
    ownership_location: str | None = None


class MediaRead(MediaBase):
    """Media read schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    original_title: str | None = None
    consumed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    genres: list[GenreRead] = []
    authors: list[AuthorRead] = []
    tags: list[TagRead] = []
    # Completeness info
    is_complete: bool = True
    missing_fields: list[str] = []
    # Book ownership
    ownership_type: OwnershipTypeEnum | None = None
    ownership_location: str | None = None
    # Book metadata (Kobo sync, progress)
    book_metadata: BookMetadataRead | None = None


class MediaListRead(BaseModel):
    """Media list schema with pagination."""

    items: list[MediaRead]
    total: int
    page: int
    page_size: int
    pages: int


class CursorPaginatedMedia(BaseModel):
    """Media list schema with cursor-based pagination.

    Cursor pagination is more efficient for large datasets and provides
    stable results when data is being modified.
    """

    items: list[MediaRead]
    next_cursor: str | None = None
    prev_cursor: str | None = None
    has_more: bool = False
    limit: int
