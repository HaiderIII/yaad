"""SQLAlchemy models."""

from src.models.base import Base
from src.models.book import BookMetadata
from src.models.film import FilmMetadata
from src.models.media import (
    Author,
    Genre,
    Media,
    MediaStatus,
    MediaType,
    Tag,
    media_authors,
    media_genres,
    media_tags,
)
from src.models.user import User
from src.models.youtube import YouTubeMetadata

__all__ = [
    "Base",
    "User",
    "Media",
    "MediaType",
    "MediaStatus",
    "Genre",
    "Author",
    "Tag",
    "media_genres",
    "media_authors",
    "media_tags",
    "FilmMetadata",
    "BookMetadata",
    "YouTubeMetadata",
]
