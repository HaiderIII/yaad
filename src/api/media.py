"""Media API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.db.crud import (
    create_media,
    create_tag,
    delete_media,
    get_incomplete_media,
    get_media,
    get_media_list,
    get_user_tags,
    update_media,
)
from src.models.media import MediaStatus, MediaType
from src.models.schemas import (
    MediaCreate,
    MediaListRead,
    MediaRead,
    MediaUpdate,
    TagCreate,
    TagRead,
)
from src.models.user import User
from src.services.metadata import tmdb_service
from src.services.metadata.youtube import youtube_service
from src.services.metadata.books import book_service
from src.services.metadata.podcast import podcast_service

router = APIRouter()


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


@router.patch("/{media_id}", response_model=MediaRead)
async def update_media_endpoint(
    media_id: int,
    data: MediaUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    authors: Annotated[list[str] | None, Query()] = None,
) -> MediaRead:
    """Update a media entry."""
    media = await update_media(
        db=db,
        media_id=media_id,
        user_id=user.id,
        data=data,
        authors=authors,
    )
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    return MediaRead.model_validate(media)


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
