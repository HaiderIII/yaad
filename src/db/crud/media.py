"""CRUD operations for media and related entities."""

import threading
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, case, func, select
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

# Genre cache with 5-minute TTL (genres rarely change)
_genre_cache: OrderedDict[str, tuple[list[Genre], datetime]] = OrderedDict()
_genre_cache_lock = threading.Lock()
_genre_cache_ttl = timedelta(minutes=5)
_genre_cache_max_size = 50


def _get_genre_cache_key(user_id: int, media_type: MediaType | None) -> str:
    """Generate cache key for genre list."""
    return f"{user_id}:{media_type.value if media_type else 'all'}"


def _get_genres_from_cache(key: str) -> list[Genre] | None:
    """Get genres from cache if not expired."""
    with _genre_cache_lock:
        if key in _genre_cache:
            genres, timestamp = _genre_cache[key]
            if datetime.now() - timestamp < _genre_cache_ttl:
                _genre_cache.move_to_end(key)
                return genres
            del _genre_cache[key]
    return None


def _set_genres_cache(key: str, genres: list[Genre]) -> None:
    """Cache genres list."""
    with _genre_cache_lock:
        if key in _genre_cache:
            _genre_cache[key] = (genres, datetime.now())
            _genre_cache.move_to_end(key)
            return
        while len(_genre_cache) >= _genre_cache_max_size:
            _genre_cache.popitem(last=False)
        _genre_cache[key] = (genres, datetime.now())


def invalidate_user_genre_cache(user_id: int) -> None:
    """Invalidate genre cache for a user (call after adding/removing media)."""
    prefix = f"{user_id}:"
    with _genre_cache_lock:
        keys_to_delete = [k for k in _genre_cache if k.startswith(prefix)]
        for key in keys_to_delete:
            del _genre_cache[key]


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
    """Create a new media entry with optional genres and authors.

    Uses transaction with rollback on any failure to ensure data consistency.
    """
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

    try:
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
            consumed_at=data.consumed_at,
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
            # Letterboxd integration
            letterboxd_slug=data.letterboxd_slug,
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

    except Exception:
        await db.rollback()
        raise


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
            selectinload(Media.book_metadata),
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
    genre: str | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    streamable_only: bool = False,
    user_platforms: set[int] | None = None,
) -> tuple[Sequence[Media], int]:
    """Get paginated list of media with filters.

    Args:
        sort_by: Field to sort by (created_at, title, year, rating, updated_at)
        sort_order: Sort direction (asc, desc)
        genre: Filter by genre name
        streamable_only: If True, only return media available on user's streaming platforms
        user_platforms: Set of provider IDs the user subscribes to (required if streamable_only=True)
    """
    query = (
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.tags),
            selectinload(Media.book_metadata),
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
    if genre:
        # Join with genres and filter
        query = query.join(media_genres).join(Genre).where(Genre.name == genre)

    # Apply sorting - IN_PROGRESS first, then TO_CONSUME, then FINISHED, then ABANDONED
    # Create priority column for status ordering
    status_priority = case(
        (Media.status == MediaStatus.IN_PROGRESS, 0),
        (Media.status == MediaStatus.TO_CONSUME, 1),
        (Media.status == MediaStatus.FINISHED, 2),
        (Media.status == MediaStatus.ABANDONED, 3),
        else_=4
    )

    sort_column = {
        "created_at": Media.created_at,
        "updated_at": Media.updated_at,
        "title": Media.title,
        "year": Media.year,
        "rating": Media.rating,
    }.get(sort_by, Media.created_at)

    # Add secondary sort by id for deterministic pagination (prevents duplicates/missing items)
    if sort_order == "asc":
        query = query.order_by(status_priority.asc(), sort_column.asc().nullslast(), Media.id.asc())
    else:
        query = query.order_by(status_priority.asc(), sort_column.desc().nullslast(), Media.id.desc())

    # Handle streamable filter
    # Optimized: Pre-filter in SQL to only fetch media that COULD have links,
    # then apply detailed JSON check in Python on the smaller result set
    if streamable_only and user_platforms:
        from sqlalchemy import or_

        # SQL pre-filter: Only fetch media that could potentially have direct links
        # - Films/Series with non-null streaming_links
        # - YouTube with external_url
        streamable_condition = or_(
            # Films/Series with streaming links
            (Media.type.in_([MediaType.FILM, MediaType.SERIES])) & (Media.streaming_links.isnot(None)),
            # YouTube with external URL
            (Media.type == MediaType.YOUTUBE) & (Media.external_url.isnot(None)),
        )
        query = query.where(streamable_condition)

        # Fetch pre-filtered items
        result = await db.execute(query)
        all_items = list(result.scalars().all())

        # Apply detailed JSON check in Python (on much smaller dataset)
        def has_direct_link(media: Media) -> bool:
            # Films/Series: check streaming platforms (flatrate)
            if media.type in (MediaType.FILM, MediaType.SERIES) and media.streaming_links:
                for provider_id, link_info in media.streaming_links.items():
                    try:
                        if int(provider_id) in user_platforms and link_info.get("type") == "flatrate":
                            return True
                    except (ValueError, TypeError):
                        continue
            # YouTube: check external_url
            if media.type == MediaType.YOUTUBE and media.external_url:
                return True
            return False

        direct_link_items = [m for m in all_items if has_direct_link(m)]
        total = len(direct_link_items)

        # Apply pagination in Python
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        items = direct_link_items[start_idx:end_idx]

        return items, total

    # Standard path: get count and paginate in DB
    count_query = select(func.count(func.distinct(Media.id))).where(Media.user_id == user_id)
    if media_type:
        count_query = count_query.where(Media.type == media_type)
    if status:
        count_query = count_query.where(Media.status == status)
    if search:
        count_query = count_query.where(
            (Media.title.ilike(f"%{search}%")) | (Media.original_title.ilike(f"%{search}%"))
        )
    if genre:
        count_query = count_query.select_from(Media).join(media_genres).join(Genre).where(Genre.name == genre)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    items = result.scalars().all()

    return items, total


async def get_genres_for_type(
    db: AsyncSession,
    user_id: int,
    media_type: MediaType | None = None,
) -> Sequence[Genre]:
    """Get all genres that have media for a user, optionally filtered by type.

    Results are cached for 5 minutes to reduce database load.
    """
    # Check cache first
    cache_key = _get_genre_cache_key(user_id, media_type)
    cached = _get_genres_from_cache(cache_key)
    if cached is not None:
        return cached

    query = (
        select(Genre)
        .join(media_genres)
        .join(Media)
        .where(Media.user_id == user_id)
        .distinct()
        .order_by(Genre.name)
    )

    if media_type:
        query = query.where(Media.type == media_type)

    result = await db.execute(query)
    genres = list(result.scalars().all())

    # Cache the result
    _set_genres_cache(cache_key, genres)

    return genres


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
            media.consumed_at = datetime.now(UTC)
    if data.rating is not None:
        media.rating = data.rating
    if data.notes is not None:
        media.notes = data.notes
    if data.current_episode is not None:
        media.current_episode = data.current_episode
        # Auto-set status to in_progress if starting to watch
        if media.current_episode > 0 and media.status == MediaStatus.TO_CONSUME:
            media.status = MediaStatus.IN_PROGRESS
        # Auto-set status to finished if reached total episodes
        if media.number_of_episodes and media.current_episode >= media.number_of_episodes:
            media.status = MediaStatus.FINISHED
            if not media.consumed_at:
                media.consumed_at = datetime.now(UTC)

    # Update ownership fields (for books)
    # When ownership_type is provided, we always update both fields together
    if data.ownership_type is not None:
        from src.models.media import OwnershipType
        media.ownership_type = OwnershipType(data.ownership_type.value)
        # Also update location (empty string or None both clear it)
        media.ownership_location = data.ownership_location if data.ownership_location else None

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
    """Get media statistics for a user with optimized single query."""
    # Single query with GROUP BY for type counts and conditional aggregation
    result = await db.execute(
        select(
            Media.type,
            func.count(Media.id).label("count"),
            func.sum(
                func.cast(Media.status == MediaStatus.IN_PROGRESS, Integer)
            ).label("in_progress_count"),
        )
        .where(Media.user_id == user_id)
        .group_by(Media.type)
    )
    rows = result.all()

    # Build stats from single query result
    type_counts = {media_type.value: 0 for media_type in MediaType}
    total = 0
    in_progress = 0

    for row in rows:
        type_counts[row.type.value] = row.count
        total += row.count
        in_progress += row.in_progress_count or 0

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
            selectinload(Media.book_metadata),
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


def _build_incomplete_condition():
    """Build SQL condition for incomplete media detection."""
    from sqlalchemy import and_, exists, not_, or_

    # Subquery to check if media has authors
    has_authors = exists(
        select(media_authors.c.media_id).where(media_authors.c.media_id == Media.id)
    )

    # Common incomplete conditions (apply to all types)
    common_incomplete = or_(
        Media.title.is_(None),
        Media.title == "",
        Media.cover_url.is_(None),
    )

    # Type-specific incomplete conditions
    type_incomplete = or_(
        # FILM: needs year, duration_minutes, authors, description
        and_(
            Media.type == MediaType.FILM,
            or_(
                Media.year.is_(None),
                Media.duration_minutes.is_(None),
                not_(has_authors),
                Media.description.is_(None),
            ),
        ),
        # SERIES: needs year, authors, description
        and_(
            Media.type == MediaType.SERIES,
            or_(
                Media.year.is_(None),
                not_(has_authors),
                Media.description.is_(None),
            ),
        ),
        # BOOK: needs year, page_count, authors, description
        and_(
            Media.type == MediaType.BOOK,
            or_(
                Media.year.is_(None),
                Media.page_count.is_(None),
                not_(has_authors),
                Media.description.is_(None),
            ),
        ),
        # YOUTUBE: needs authors, external_url
        and_(
            Media.type == MediaType.YOUTUBE,
            or_(
                not_(has_authors),
                Media.external_url.is_(None),
            ),
        ),
        # PODCAST: needs authors, description
        and_(
            Media.type == MediaType.PODCAST,
            or_(
                not_(has_authors),
                Media.description.is_(None),
            ),
        ),
        # SHOW: needs year, description
        and_(
            Media.type == MediaType.SHOW,
            or_(
                Media.year.is_(None),
                Media.description.is_(None),
            ),
        ),
    )

    return or_(common_incomplete, type_incomplete)


async def get_incomplete_media(
    db: AsyncSession,
    user_id: int,
    media_type: MediaType | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Media], int]:
    """
    Get media with missing essential fields using SQL filtering.

    Uses SQL CASE expressions to check completeness per media type,
    avoiding loading all media into memory.
    """
    is_incomplete = _build_incomplete_condition()

    # Build base filter
    base_filter = [Media.user_id == user_id, is_incomplete]
    if media_type:
        base_filter.append(Media.type == media_type)

    # Count query
    count_result = await db.execute(
        select(func.count(Media.id)).where(*base_filter)
    )
    total = count_result.scalar() or 0

    # Data query with pagination
    query = (
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.tags),
            selectinload(Media.book_metadata),
        )
        .where(*base_filter)
        .order_by(Media.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(query)
    items = result.scalars().all()

    return items, total


async def get_incomplete_count(
    db: AsyncSession,
    user_id: int,
) -> int:
    """Get count of incomplete media for a user using SQL filtering."""
    is_incomplete = _build_incomplete_condition()
    result = await db.execute(
        select(func.count(Media.id)).where(Media.user_id == user_id, is_incomplete)
    )
    return result.scalar() or 0


async def get_unfinished_media(
    db: AsyncSession,
    user_id: int,
    limit: int = 100,
) -> list[Media]:
    """
    Get media that are not finished (to_consume, in_progress, or abandoned).

    Returns a heterogeneous mix of all media types, interleaving different
    categories to provide variety. Prioritizes in_progress first.
    """
    result = await db.execute(
        select(Media)
        .options(
            selectinload(Media.genres),
            selectinload(Media.authors),
            selectinload(Media.book_metadata),
        )
        .where(
            Media.user_id == user_id,
            Media.status != MediaStatus.FINISHED,
        )
        .order_by(
            # Prioritize in_progress, then to_consume
            (Media.status == MediaStatus.IN_PROGRESS).desc(),
            (Media.status == MediaStatus.TO_CONSUME).desc(),
            Media.updated_at.desc(),
        )
    )
    all_media = list(result.scalars().all())

    # Group by type
    by_type: dict[MediaType, list[Media]] = {}
    for media in all_media:
        if media.type not in by_type:
            by_type[media.type] = []
        by_type[media.type].append(media)

    # Interleave types for heterogeneous display
    # Round-robin through types to create variety
    shuffled: list[Media] = []
    type_iters = {t: iter(items) for t, items in by_type.items()}
    types_order = list(by_type.keys())

    while len(shuffled) < limit and type_iters:
        for media_type in list(types_order):
            if media_type not in type_iters:
                continue
            try:
                item = next(type_iters[media_type])
                shuffled.append(item)
                if len(shuffled) >= limit:
                    break
            except StopIteration:
                del type_iters[media_type]
                types_order.remove(media_type)

    return shuffled
