"""Search API endpoints."""

import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.auth import get_current_user
from src.constants import CACHE_TTL_SEARCH, SEARCH_CACHE_MAX_SIZE
from src.db import get_db
from src.models.media import Author, Media, media_authors
from src.models.user import User

router = APIRouter()

# Thread-safe LRU cache with TTL (O(1) operations)
_search_cache: OrderedDict[str, tuple[Any, datetime]] = OrderedDict()
_cache_lock = threading.Lock()
_cache_ttl = timedelta(seconds=CACHE_TTL_SEARCH)
_cache_max_size = SEARCH_CACHE_MAX_SIZE


def _get_cache_key(user_id: int, query: str, limit: int) -> str:
    """Generate cache key for search."""
    return f"{user_id}:{query.lower()}:{limit}"


def _get_from_cache(key: str) -> Any | None:
    """Get value from cache if not expired (thread-safe, O(1))."""
    with _cache_lock:
        if key in _search_cache:
            value, timestamp = _search_cache[key]
            if datetime.now() - timestamp < _cache_ttl:
                # Move to end (most recently used)
                _search_cache.move_to_end(key)
                return value
            # Expired, remove it
            del _search_cache[key]
    return None


def _set_cache(key: str, value: Any) -> None:
    """Set value in cache with LRU eviction (thread-safe, O(1))."""
    with _cache_lock:
        # If key exists, update and move to end
        if key in _search_cache:
            _search_cache[key] = (value, datetime.now())
            _search_cache.move_to_end(key)
            return
        # Evict oldest entries if at capacity (FIFO from front)
        while len(_search_cache) >= _cache_max_size:
            _search_cache.popitem(last=False)  # O(1) removal from front
        _search_cache[key] = (value, datetime.now())


def invalidate_user_search_cache(user_id: int) -> None:
    """Invalidate all search cache entries for a user (thread-safe)."""
    prefix = f"{user_id}:"
    with _cache_lock:
        keys_to_delete = [k for k in _search_cache if k.startswith(prefix)]
        for key in keys_to_delete:
            del _search_cache[key]


class SearchResult(BaseModel):
    """Search result item."""

    id: int
    title: str
    type: str
    type_label: str
    year: int | None
    cover_url: str | None
    rating: float | None


class SearchResponse(BaseModel):
    """Search response."""

    results: list[SearchResult]
    total: int


TYPE_LABELS = {
    "film": "Film",
    "series": "Series",
    "book": "Book",
    "youtube": "Video",
    "podcast": "Podcast",
    "show": "Show",
}


@router.get("", response_model=SearchResponse)
async def search_media(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
    user: Annotated[User, Depends(get_current_user)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> SearchResponse:
    """Search media by title, original title, or author name."""
    search_term = q.strip().lower()

    # Check cache first
    cache_key = _get_cache_key(user.id, search_term, limit)
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    # First, find matching media IDs (including author search)
    subquery = (
        select(Media.id)
        .distinct()
        .outerjoin(media_authors, Media.id == media_authors.c.media_id)
        .outerjoin(Author, media_authors.c.author_id == Author.id)
        .where(
            Media.user_id == user.id,
            or_(
                Media.title.ilike(f"%{search_term}%"),
                Media.original_title.ilike(f"%{search_term}%"),
                Author.name.ilike(f"%{search_term}%"),
            ),
        )
    ).subquery()

    # Then fetch full media objects with proper ordering
    query = (
        select(Media)
        .options(selectinload(Media.genres), selectinload(Media.authors))
        .where(Media.id.in_(select(subquery.c.id)))
        .order_by(
            # Prioritize exact title matches at start
            Media.title.ilike(f"{search_term}%").desc(),
            Media.title.asc(),
        )
        .limit(limit)
    )

    result = await db.execute(query)
    media_list = result.scalars().all()

    results = [
        SearchResult(
            id=media.id,
            title=media.title,
            type=media.type.value,
            type_label=TYPE_LABELS.get(media.type.value, media.type.value),
            year=media.year,
            cover_url=media.cover_url,
            rating=media.rating,
        )
        for media in media_list
    ]

    response = SearchResponse(results=results, total=len(results))

    # Cache the response
    _set_cache(cache_key, response)

    return response
