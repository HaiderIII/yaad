"""SQLAlchemy models."""

from src.models.base import Base
from src.models.user import User
from src.models.media import (
    Media,
    MediaType,
    MediaStatus,
    Genre,
    Author,
    Tag,
    media_genres,
    media_authors,
    media_tags,
)
from src.models.film import FilmMetadata
from src.models.book import BookMetadata
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
