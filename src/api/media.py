"""Media API endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.search import invalidate_user_search_cache
from src.auth import get_current_user
from src.db import get_db
from src.db.crud import (
    create_media,
    create_tag,
    delete_media,
    get_incomplete_media,
    get_media,
    get_media_list,
    get_media_list_cursor,
    get_user_tags,
    update_media,
    update_media_quick,
)
from src.models.media import MediaStatus, MediaType
from src.models.schemas import (
    CursorPaginatedMedia,
    MediaCreate,
    MediaListRead,
    MediaRead,
    MediaUpdate,
    TagCreate,
    TagRead,
)
from src.models.user import User
from src.services.metadata import tmdb_service
from src.services.metadata.books import book_service
from src.services.metadata.podcast import podcast_service
from src.services.metadata.youtube import youtube_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("", response_model=MediaRead, status_code=201)
async def create_media_endpoint(
    data: MediaCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    genres: Annotated[list[str] | None, Query()] = None,
    authors: Annotated[list[str] | None, Query()] = None,
) -> MediaRead:
    """Create a new media entry."""
    media = await create_media(
        db=db,
        user_id=user.id,
        data=data,
        genres=genres,
        authors=authors,
    )
    # Invalidate search cache for this user
    invalidate_user_search_cache(user.id)
    return MediaRead.model_validate(media)


@router.get("", response_model=MediaListRead)
async def list_media(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MediaListRead:
    """List media with optional filters."""
    media_type = MediaType(type) if type else None
    media_status = MediaStatus(status) if status else None

    items, total = await get_media_list(
        db=db,
        user_id=user.id,
        media_type=media_type,
        status=media_status,
        search=search,
        page=page,
        page_size=page_size,
    )

    pages = (total + page_size - 1) // page_size if total > 0 else 0

    return MediaListRead(
        items=[MediaRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/cursor", response_model=CursorPaginatedMedia)
async def list_media_cursor(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_order: Annotated[str, Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> CursorPaginatedMedia:
    """List media with cursor-based pagination.

    Cursor pagination is more efficient for large datasets and provides
    stable results when data is being modified. Use the `cursor` parameter
    with the `next_cursor` value from the previous response to get the next page.
    """
    media_type = MediaType(type) if type else None
    media_status = MediaStatus(status) if status else None

    items, next_cursor, has_more = await get_media_list_cursor(
        db=db,
        user_id=user.id,
        media_type=media_type,
        status=media_status,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        cursor=cursor,
    )

    return CursorPaginatedMedia(
        items=[MediaRead.model_validate(item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
    )


@router.get("/incomplete", response_model=MediaListRead)
async def list_incomplete_media(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MediaListRead:
    """List media with missing essential fields."""
    media_type = MediaType(type) if type else None

    items, total = await get_incomplete_media(
        db=db,
        user_id=user.id,
        media_type=media_type,
        page=page,
        page_size=page_size,
    )

    pages = (total + page_size - 1) // page_size if total > 0 else 0

    return MediaListRead(
        items=[MediaRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/lookup/youtube")
async def lookup_youtube(
    url: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Get YouTube video info from URL or video ID."""
    result = await youtube_service.get_video_info(url)
    if not result:
        raise HTTPException(status_code=404, detail="Video not found or invalid URL")
    return result


@router.get("/lookup/book")
async def lookup_book(
    isbn: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Get book info from ISBN."""
    result = await book_service.search_by_isbn(isbn)
    if not result:
        raise HTTPException(status_code=404, detail="Book not found")
    return result


@router.get("/search/books")
async def search_books(
    query: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> list[dict]:
    """Search books by title/author."""
    results = await book_service.search_books(query=query, limit=limit)
    return results


@router.get("/lookup/podcast")
async def lookup_podcast(
    url: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Get podcast episode info from URL (Spotify, Apple, RSS, etc.)."""
    result = await podcast_service.get_episode_info(url)
    if not result:
        raise HTTPException(status_code=404, detail="Podcast not found or invalid URL")
    return result


@router.get("/search/podcasts")
async def search_podcasts(
    query: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> list[dict]:
    """Search podcasts by name."""
    results = await podcast_service.search_podcasts(query=query, limit=limit)
    return results


@router.get("/podcast/episodes")
async def get_podcast_episodes(
    feed_url: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[dict]:
    """Get episodes from a podcast RSS feed."""
    episodes = await podcast_service.get_show_episodes(feed_url=feed_url, limit=limit)
    return episodes


@router.get("/search/tmdb")
async def search_tmdb(
    query: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
    year: Annotated[int | None, Query()] = None,
) -> list[dict]:
    """Search TMDB for movies."""
    results = await tmdb_service.search_movies(query=query, year=year)
    return results


@router.get("/search/tmdb/tv")
async def search_tmdb_tv(
    query: Annotated[str, Query(min_length=1)],
    user: Annotated[User, Depends(get_current_user)],
    year: Annotated[int | None, Query()] = None,
) -> list[dict]:
    """Search TMDB for TV series."""
    results = await tmdb_service.search_tv(query=query, year=year)
    return results


@router.get("/tmdb/tv/{tmdb_id}")
async def get_tmdb_tv_details(
    tmdb_id: int,
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Get detailed TV series info from TMDB."""
    details = await tmdb_service.get_tv_details(tmdb_id)
    if not details:
        raise HTTPException(status_code=404, detail="TV series not found")
    return details


@router.get("/tmdb/{tmdb_id}")
async def get_tmdb_details(
    tmdb_id: int,
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Get detailed movie info from TMDB."""
    details = await tmdb_service.get_movie_details(tmdb_id)
    if not details:
        raise HTTPException(status_code=404, detail="Movie not found")
    return details


@router.get("/{media_id}", response_model=MediaRead)
async def get_media_endpoint(
    media_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaRead:
    """Get a single media by ID."""
    media = await get_media(db=db, media_id=media_id, user_id=user.id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    return MediaRead.model_validate(media)


@router.patch("/{media_id}/progress")
async def update_media_progress(
    media_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[str | None, Query()] = None,
    rating: Annotated[float | None, Query()] = None,
    current_episode: Annotated[int | None, Query()] = None,
) -> dict:
    """Fast endpoint for updating progress (status, rating, episode).

    Optimized for rapid updates - no full re-fetch of media relationships.
    """
    result = await update_media_quick(
        db=db,
        media_id=media_id,
        user_id=user.id,
        status=status,
        rating=rating,
        current_episode=current_episode,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Media not found")

    return result


@router.patch("/{media_id}", response_model=MediaRead)
async def update_media_endpoint(
    media_id: int,
    data: MediaUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
    authors: Annotated[list[str] | None, Query()] = None,
) -> MediaRead:
    """Update a media entry."""
    # Get current status before update to detect status change
    old_media = await get_media(db=db, media_id=media_id, user_id=user.id)
    old_status = old_media.status if old_media else None

    media = await update_media(
        db=db,
        media_id=media_id,
        user_id=user.id,
        data=data,
        authors=authors,
    )
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    # If YouTube video marked as consumed, remove from playlist in background
    if (
        media.type == MediaType.YOUTUBE
        and data.status == MediaStatus.CONSUMED
        and old_status != MediaStatus.CONSUMED
    ):
        background_tasks.add_task(_remove_youtube_from_playlist, media.id, user.id)

    # Invalidate search cache for this user
    invalidate_user_search_cache(user.id)
    return MediaRead.model_validate(media)


async def _remove_youtube_from_playlist(media_id: int, user_id: int) -> None:
    """Background task to remove YouTube video from playlist."""
    from src.db import async_session_maker
    from src.services.youtube import remove_video_from_playlist

    async with async_session_maker() as db:
        media = await get_media(db=db, media_id=media_id, user_id=user_id)
        user = await db.get(User, user_id)
        if media and user:
            try:
                await remove_video_from_playlist(db, media, user)
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to remove YouTube video from playlist: {e}")


@router.delete("/{media_id}", status_code=204)
async def delete_media_endpoint(
    media_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a media entry."""
    deleted = await delete_media(db=db, media_id=media_id, user_id=user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Media not found")
    # Invalidate search cache for this user
    invalidate_user_search_cache(user.id)


# Tag endpoints
@router.get("/tags/list", response_model=list[TagRead])
async def list_tags(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TagRead]:
    """List all user tags."""
    tags = await get_user_tags(db=db, user_id=user.id)
    return [TagRead.model_validate(tag) for tag in tags]


@router.post("/tags", response_model=TagRead, status_code=201)
async def create_tag_endpoint(
    data: TagCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TagRead:
    """Create a new tag."""
    tag = await create_tag(db=db, user_id=user.id, name=data.name)
    return TagRead.model_validate(tag)


# JustWatch health check endpoint
@router.get("/streaming/health")
async def justwatch_health_check(
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Check if JustWatch API is working correctly.

    This endpoint tests the JustWatch GraphQL API with a known movie
    to verify that deep links can still be fetched.
    Run this periodically (weekly) to detect API changes.
    """
    from src.services.metadata.justwatch import justwatch_service

    result = await justwatch_service.health_check()
    return result
