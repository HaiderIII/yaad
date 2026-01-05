"""CRUD operations for media and related entities."""

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from src.models.schemas import MediaCreate, MediaUpdate


async def get_or_create_genre(
    db: AsyncSession,
    name: str,
    media_type: MediaType,
) -> Genre:
    """Get existing genre or create new one."""
    result = await db.execute(
        select(Genre).where(Genre.name == name, Genre.media_type == media_type)
    )
    genre = result.scalar_one_or_none()

    if not genre:
        genre = Genre(name=name, media_type=media_type)
        db.add(genre)
        await db.flush()

    return genre


async def get_or_create_author(
    db: AsyncSession,
    name: str,
    media_type: MediaType,
    external_id: str | None = None,
) -> Author:
    """Get existing author or create new one."""
    result = await db.execute(
        select(Author).where(Author.name == name, Author.media_type == media_type)
    )
    author = result.scalar_one_or_none()

    if not author:
        author = Author(name=name, media_type=media_type, external_id=external_id)
        db.add(author)
        await db.flush()

    return author


async def create_media(
    db: AsyncSession,
    user_id: int,
    data: MediaCreate,
    genres: list[str] | None = None,
    authors: list[str] | None = None,
) -> Media:
    """Create a new media entry with optional genres and authors."""
    # Handle title logic:
    # - data.title = original title (from API)
    # - data.local_title = French/local title (if different)
    # - In DB: title = display title (local if available, else original)
    #          original_title = original title (if different from display)
    if data.local_title and data.local_title != data.title:
        display_title = data.local_title
        original_title = data.title
    else:
        display_title = data.title
        original_title = None

    media = Media(
        user_id=user_id,
        type=MediaType(data.type.value),
        title=display_title,
        original_title=original_title,
        external_id=data.external_id,
        year=data.year,
        duration_minutes=data.duration_minutes,
        page_count=data.page_count,
        description=data.description,
        cover_url=data.cover_url,
        external_url=data.external_url,
        status=MediaStatus(data.status.value),
        rating=data.rating,
        notes=data.notes,
        # Extended metadata (TMDB)
        tmdb_rating=data.tmdb_rating,
        tmdb_vote_count=data.tmdb_vote_count,
        popularity=data.popularity,
        budget=data.budget,
        revenue=data.revenue,
        original_language=data.original_language,
        production_countries=data.production_countries,
        cast=data.cast,
        keywords=data.keywords,
        collection_id=data.collection_id,
        collection_name=data.collection_name,
        certification=data.certification,
        tagline=data.tagline,
        # Series-specific
        number_of_seasons=data.number_of_seasons,
        number_of_episodes=data.number_of_episodes,
        series_status=data.series_status,
        networks=data.networks,
    )

    db.add(media)
    await db.flush()

    # Add genres by name using direct INSERT into association table
    if genres:
        media_type = MediaType(data.type.value)
        for genre_name in genres:
            genre = await get_or_create_genre(db, genre_name, media_type)
            await db.execute(
                media_genres.insert().values(media_id=media.id, genre_id=genre.id)
            )

    # Add authors by name using direct INSERT into association table
    if authors:
        media_type = MediaType(data.type.value)
        for author_name in authors:
            author = await get_or_create_author(db, author_name, media_type)
            await db.execute(
                media_authors.insert().values(media_id=media.id, author_id=author.id)
            )

    # Add tags by ID (user's existing tags) using direct INSERT
    if data.tag_ids:
        result = await db.execute(
            select(Tag).where(Tag.id.in_(data.tag_ids), Tag.user_id == user_id)
        )
        tags = result.scalars().all()
        for tag in tags:
            await db.execute(
                media_tags.insert().values(media_id=media.id, tag_id=tag.id)
            )

    await db.commit()

    # Reload media with all relationships
    reloaded = await get_media(db, media.id, user_id)
    if not reloaded:
        raise RuntimeError(f"Failed to reload media {media.id}")
    return reloaded


async def get_media(
    db: AsyncSession,
    media_id: int,
    user_id: int,
) -> Media | None:
    """Get a single media by ID with user isolation."""
    result = await db.execute(
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.tags),
        )
        .where(Media.id == media_id, Media.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_media_list(
    db: AsyncSession,
    user_id: int,
    media_type: MediaType | None = None,
    status: MediaStatus | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Media], int]:
    """Get paginated list of media with filters."""
    query = (
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.tags),
        )
        .where(Media.user_id == user_id)
    )

    # Apply filters
    if media_type:
        query = query.where(Media.type == media_type)
    if status:
        query = query.where(Media.status == status)
    if search:
        # Search in both display title and original title
        query = query.where(
            (Media.title.ilike(f"%{search}%")) | (Media.original_title.ilike(f"%{search}%"))
        )

    # Get total count
    count_query = select(func.count(Media.id)).where(Media.user_id == user_id)
    if media_type:
        count_query = count_query.where(Media.type == media_type)
    if status:
        count_query = count_query.where(Media.status == status)
    if search:
        count_query = count_query.where(
            (Media.title.ilike(f"%{search}%")) | (Media.original_title.ilike(f"%{search}%"))
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = query.order_by(Media.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    items = result.scalars().all()

    return items, total


async def update_media(
    db: AsyncSession,
    media_id: int,
    user_id: int,
    data: MediaUpdate,
    authors: list[str] | None = None,
) -> Media | None:
    """Update a media entry."""
    media = await get_media(db, media_id, user_id)
    if not media:
        return None

    # Update fields if provided
    if data.title is not None:
        media.title = data.title
    if data.year is not None:
        media.year = data.year
    if data.duration_minutes is not None:
        media.duration_minutes = data.duration_minutes
    if data.page_count is not None:
        media.page_count = data.page_count
    if data.description is not None:
        media.description = data.description
    if data.cover_url is not None:
        media.cover_url = data.cover_url
    if data.external_id is not None:
        media.external_id = data.external_id
    if data.external_url is not None:
        media.external_url = data.external_url
    if data.status is not None:
        media.status = MediaStatus(data.status.value)
        # Set consumed_at when finished
        if data.status.value == "finished" and not media.consumed_at:
            media.consumed_at = datetime.now(timezone.utc)
    if data.rating is not None:
        media.rating = data.rating
    if data.notes is not None:
        media.notes = data.notes

    # Update tags if provided
    if data.tag_ids is not None:
        result = await db.execute(
            select(Tag).where(Tag.id.in_(data.tag_ids), Tag.user_id == user_id)
        )
        tags = list(result.scalars().all())
        media.tags = tags

    # Update authors if provided (by name)
    if authors is not None:
        # Clear existing authors and add new ones
        media.authors = []
        await db.flush()  # Ensure the relationship is cleared

        for author_name in authors:
            author = await get_or_create_author(db, author_name, media.type)
            media.authors.append(author)

    await db.commit()
    await db.refresh(media)

    return media


async def delete_media(
    db: AsyncSession,
    media_id: int,
    user_id: int,
) -> bool:
    """Delete a media entry."""
    media = await get_media(db, media_id, user_id)
    if not media:
        return False

    await db.delete(media)
    await db.commit()
    return True


async def get_user_stats(
    db: AsyncSession,
    user_id: int,
) -> dict:
    """Get media statistics for a user."""
    # Count by type
    type_counts = {}
    for media_type in MediaType:
        result = await db.execute(
            select(func.count(Media.id)).where(
                Media.user_id == user_id, Media.type == media_type
            )
        )
        type_counts[media_type.value] = result.scalar() or 0

    # Count in progress
    result = await db.execute(
        select(func.count(Media.id)).where(
            Media.user_id == user_id, Media.status == MediaStatus.IN_PROGRESS
        )
    )
    in_progress = result.scalar() or 0

    # Total count
    result = await db.execute(
        select(func.count(Media.id)).where(Media.user_id == user_id)
    )
    total = result.scalar() or 0

    return {
        "films": type_counts.get("film", 0),
        "books": type_counts.get("book", 0),
        "videos": type_counts.get("youtube", 0),
        "podcasts": type_counts.get("podcast", 0),
        "shows": type_counts.get("show", 0),
        "series": type_counts.get("series", 0),
        "in_progress": in_progress,
        "total": total,
    }


async def get_recent_media(
    db: AsyncSession,
    user_id: int,
    limit: int = 6,
) -> Sequence[Media]:
    """Get recently added media."""
    result = await db.execute(
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
        )
        .where(Media.user_id == user_id)
        .order_by(Media.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def get_user_tags(
    db: AsyncSession,
    user_id: int,
) -> Sequence[Tag]:
    """Get all tags for a user."""
    result = await db.execute(
        select(Tag).where(Tag.user_id == user_id).order_by(Tag.name)
    )
    return result.scalars().all()


async def create_tag(
    db: AsyncSession,
    user_id: int,
    name: str,
) -> Tag:
    """Create a new tag for a user."""
    tag = Tag(user_id=user_id, name=name)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


async def get_incomplete_media(
    db: AsyncSession,
    user_id: int,
    media_type: MediaType | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Media], int]:
    """
    Get media with missing essential fields.

    This fetches all media and filters in Python since completeness
    depends on type-specific logic that can't be efficiently expressed in SQL.
    """
    # Build base query
    query = (
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.tags),
        )
        .where(Media.user_id == user_id)
    )

    if media_type:
        query = query.where(Media.type == media_type)

    # Order by created_at (newest first)
    query = query.order_by(Media.created_at.desc())

    # Execute query
    result = await db.execute(query)
    all_media = result.scalars().all()

    # Filter incomplete media in Python (uses the is_complete property)
    incomplete = [m for m in all_media if not m.is_complete]

    # Get total count
    total = len(incomplete)

    # Apply pagination
    start = (page - 1) * page_size
    end = start + page_size
    paginated = incomplete[start:end]

    return paginated, total


async def get_incomplete_count(
    db: AsyncSession,
    user_id: int,
) -> int:
    """Get count of incomplete media for a user."""
    result = await db.execute(
        select(Media)
        .options(
            selectinload(Media.authors),
        )
        .where(Media.user_id == user_id)
    )
    all_media = result.scalars().all()
    return sum(1 for m in all_media if not m.is_complete)


async def get_unfinished_media(
    db: AsyncSession,
    user_id: int,
    limit: int = 20,
) -> Sequence[Media]:
    """
    Get media that are not finished (to_consume, in_progress, or abandoned).

    Returns a mix of all media types, prioritizing in_progress first,
    then to_consume, ordered by most recently updated.
    """
    result = await db.execute(
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
        )
        .where(
            Media.user_id == user_id,
            Media.status != MediaStatus.FINISHED,
        )
        .order_by(
            # Prioritize in_progress, then to_consume, then abandoned
            (Media.status == MediaStatus.IN_PROGRESS).desc(),
            (Media.status == MediaStatus.TO_CONSUME).desc(),
            Media.updated_at.desc(),
        )
        .limit(limit)
    )
    return result.scalars().all()
