"""CRUD operations module."""

from src.db.crud.media import (
    create_media,
    create_tag,
    delete_media,
    get_genres_for_type,
    get_incomplete_count,
    get_incomplete_media,
    get_media,
    get_media_list,
    get_or_create_author,
    get_or_create_genre,
    get_recent_media,
    get_unfinished_media,
    get_user_stats,
    get_user_tags,
    invalidate_user_genre_cache,
    update_media,
)

__all__ = [
    "create_media",
    "create_tag",
    "delete_media",
    "get_genres_for_type",
    "get_incomplete_count",
    "get_incomplete_media",
    "get_media",
    "get_media_list",
    "get_or_create_author",
    "get_or_create_genre",
    "get_recent_media",
    "get_unfinished_media",
    "get_user_stats",
    "get_user_tags",
    "invalidate_user_genre_cache",
    "update_media",
]
