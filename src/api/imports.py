"""Import API endpoints."""

import asyncio
import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.search import invalidate_user_search_cache
from src.auth import get_current_user
from src.db import get_db
from src.models.user import User
from src.services.imports.letterboxd import LetterboxdEntry, letterboxd_importer
from src.services.imports.letterboxd_sync import letterboxd_sync
from src.services.imports.notion import notion_importer

logger = logging.getLogger(__name__)
router = APIRouter()


class ImportResponse(BaseModel):
    """Import operation response."""

    imported: int
    skipped: int
    failed: int
    errors: list[str] | None = None


@router.post("/letterboxd", response_model=ImportResponse)
async def import_letterboxd(
    file: Annotated[UploadFile, File(description="Letterboxd CSV export (diary.csv or watched.csv)")],
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip_existing: bool = True,
    fetch_metadata: bool = True,
) -> ImportResponse:
    """Import films from Letterboxd CSV export.

    Accepts either diary.csv or watched.csv from Letterboxd export.
    Films are matched with TMDB to fetch full metadata.

    Args:
        file: CSV file upload
        skip_existing: Skip films already in library (default: True)
        fetch_metadata: Fetch full metadata from TMDB (default: True)
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    # Read file content
    content = await file.read()
    try:
        csv_content = content.decode("utf-8")
    except UnicodeDecodeError:
        # Try latin-1 as fallback
        csv_content = content.decode("latin-1")

    # Detect file type from filename
    file_type = "diary" if "diary" in file.filename.lower() else "watched"

    # Parse CSV
    entries = letterboxd_importer.parse_csv(csv_content, file_type)

    if not entries:
        raise HTTPException(status_code=400, detail="No valid entries found in CSV")

    # Import entries
    result = await letterboxd_importer.import_entries(
        db=db,
        user_id=user.id,
        entries=entries,
        skip_existing=skip_existing,
        fetch_metadata=fetch_metadata,
        force_update=False,  # CSV import doesn't force update
    )

    # Invalidate search cache
    if result.imported > 0:
        invalidate_user_search_cache(user.id)

    return ImportResponse(
        imported=result.imported,
        skipped=result.skipped,
        failed=result.failed,
        errors=result.errors[:10] if result.errors else None,  # Limit errors returned
    )


class LetterboxdValidateResponse(BaseModel):
    """Letterboxd username validation response."""

    valid: bool
    username: str


class LetterboxdSyncResponse(BaseModel):
    """Letterboxd sync response."""

    imported: int
    skipped: int
    failed: int
    total_found: int
    sync_type: str  # "rss" or "full"


@router.get("/letterboxd/validate", response_model=LetterboxdValidateResponse)
async def validate_letterboxd_username(
    username: str,
    user: Annotated[User, Depends(get_current_user)],
) -> LetterboxdValidateResponse:
    """Validate a Letterboxd username exists."""
    # Clean username
    username = username.strip().lower()
    if username.startswith("@"):
        username = username[1:]

    valid = await letterboxd_sync.validate_username(username)
    return LetterboxdValidateResponse(valid=valid, username=username)


@router.post("/letterboxd/sync", response_model=LetterboxdSyncResponse)
async def sync_letterboxd(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    full_import: bool = False,
    skip_existing: bool = True,
    fetch_metadata: bool = True,
    force_update: bool = False,
) -> LetterboxdSyncResponse:
    """Sync films from Letterboxd.

    Args:
        full_import: If True, scrape all films. If False, use RSS (last ~50).
        skip_existing: Skip films already in library.
        fetch_metadata: Fetch full metadata from TMDB.
        force_update: Force update ratings even if local rating exists.
    """
    # Capture user data early to avoid session issues after rollbacks
    user_id = user.id
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    # Fetch films from Letterboxd
    if full_import:
        logger.info(f"Starting full Letterboxd import for {username}")
        films = await letterboxd_sync.scrape_all_films(username, include_ratings=True)
        sync_type = "full"
    else:
        logger.info(f"Starting RSS sync for {username}")
        films = await letterboxd_sync.fetch_rss(username)
        sync_type = "rss"

    if not films:
        return LetterboxdSyncResponse(
            imported=0,
            skipped=0,
            failed=0,
            total_found=0,
            sync_type=sync_type,
        )

    # Convert to LetterboxdEntry format for the importer
    entries = [
        LetterboxdEntry(
            name=film.title,
            year=film.year,
            rating=film.rating,
            watched_date=film.watched_date,
            letterboxd_uri=film.letterboxd_uri,
            rewatch=film.rewatch,
        )
        for film in films
    ]

    # Import using existing importer
    result = await letterboxd_importer.import_entries(
        db=db,
        user_id=user_id,
        entries=entries,
        skip_existing=skip_existing,
        fetch_metadata=fetch_metadata,
        force_update=force_update,
    )

    # Invalidate search cache
    if result.imported > 0:
        invalidate_user_search_cache(user_id)

    return LetterboxdSyncResponse(
        imported=result.imported,
        skipped=result.skipped,
        failed=result.failed,
        total_found=len(films),
        sync_type=sync_type,
    )


@router.get("/letterboxd/sync-stream")
async def sync_letterboxd_stream(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    full_import: bool = False,
    skip_existing: bool = True,
    fetch_metadata: bool = True,
    force_update: bool = False,
):
    """Sync films from Letterboxd with progress streaming via SSE.

    Args:
        force_update: Force update ratings even if local rating exists.

    Returns Server-Sent Events with progress updates.
    """
    # Capture user data early
    user_id = user.id
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    async def generate():
        """Generate SSE events for progress."""
        from src.db import async_session_maker

        try:
            # Phase 1: Scraping Letterboxd
            yield f"data: {json.dumps({'phase': 'scraping', 'message': 'Fetching films from Letterboxd...'})}\n\n"

            if full_import:
                films = await letterboxd_sync.scrape_all_films(username, include_ratings=True)
                sync_type = "full"
            else:
                films = await letterboxd_sync.fetch_rss(username)
                sync_type = "rss"

            total = len(films)
            if not films:
                yield f"data: {json.dumps({'phase': 'done', 'imported': 0, 'skipped': 0, 'failed': 0, 'total_found': 0, 'sync_type': sync_type})}\n\n"
                return

            yield f"data: {json.dumps({'phase': 'importing', 'message': f'Found {total} films. Starting import...', 'total': total, 'current': 0})}\n\n"

            # Phase 2: Import with progress
            entries = [
                LetterboxdEntry(
                    name=film.title,
                    year=film.year,
                    rating=film.rating,
                    watched_date=film.watched_date,
                    letterboxd_uri=film.letterboxd_uri,
                    rewatch=film.rewatch,
                )
                for film in films
            ]

            # Import with progress callback
            imported = 0
            skipped = 0
            failed = 0
            errors: list[str] = []

            async with async_session_maker() as db:
                for i, entry in enumerate(entries):
                    # Check if client disconnected
                    if await request.is_disconnected():
                        return

                    status, error_msg = await letterboxd_importer.import_single_entry(
                        db=db,
                        user_id=user_id,
                        entry=entry,
                        skip_existing=skip_existing,
                        fetch_metadata=fetch_metadata,
                        force_update=force_update,
                    )

                    if status in ("imported", "updated"):
                        imported += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        if error_msg:
                            errors.append(error_msg)

                    # Send progress every film
                    progress_data = {
                        "phase": "importing",
                        "current": i + 1,
                        "total": total,
                        "imported": imported,
                        "skipped": skipped,
                        "failed": failed,
                        "current_film": entry.name,
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                    # Small delay to prevent overwhelming
                    await asyncio.sleep(0.01)

                # Final commit
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()

            # Invalidate search cache
            if imported > 0:
                invalidate_user_search_cache(user_id)

            # Final result with errors
            yield f"data: {json.dumps({'phase': 'done', 'imported': imported, 'skipped': skipped, 'failed': failed, 'total_found': total, 'sync_type': sync_type, 'errors': errors[:20]})}\n\n"

        except Exception as e:
            logger.exception("Error during Letterboxd sync stream")
            yield f"data: {json.dumps({'phase': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class WatchlistSyncResponse(BaseModel):
    """Watchlist sync response."""

    imported: int
    skipped: int
    failed: int
    total_found: int


@router.post("/letterboxd/watchlist", response_model=WatchlistSyncResponse)
async def sync_letterboxd_watchlist(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip_existing: bool = True,
    fetch_metadata: bool = True,
) -> WatchlistSyncResponse:
    """Import watchlist from Letterboxd.

    Imports all films from the user's Letterboxd watchlist as "To Watch" items.

    Args:
        skip_existing: Skip films already in library.
        fetch_metadata: Fetch full metadata from TMDB.
    """
    # Capture user data early
    user_id = user.id
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    # Fetch watchlist from Letterboxd
    logger.info(f"Fetching watchlist for {username}")
    films = await letterboxd_sync.scrape_watchlist(username)

    if not films:
        return WatchlistSyncResponse(
            imported=0,
            skipped=0,
            failed=0,
            total_found=0,
        )

    # Convert to LetterboxdEntry format for the importer
    # Watchlist items have no rating or watched date
    entries = [
        LetterboxdEntry(
            name=film.title,
            year=film.year,
            rating=None,
            watched_date=None,
            letterboxd_uri=film.letterboxd_uri,
            rewatch=False,
        )
        for film in films
    ]

    # Import using existing importer
    result = await letterboxd_importer.import_entries(
        db=db,
        user_id=user_id,
        entries=entries,
        skip_existing=skip_existing,
        fetch_metadata=fetch_metadata,
    )

    # Invalidate search cache
    if result.imported > 0:
        invalidate_user_search_cache(user_id)

    return WatchlistSyncResponse(
        imported=result.imported,
        skipped=result.skipped,
        failed=result.failed,
        total_found=len(films),
    )


@router.get("/letterboxd/watchlist-stream")
async def sync_letterboxd_watchlist_stream(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    skip_existing: bool = True,
    fetch_metadata: bool = True,
):
    """Import watchlist from Letterboxd with progress streaming via SSE."""
    # Capture user data early
    user_id = user.id
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    async def generate():
        """Generate SSE events for progress."""
        from src.db import async_session_maker

        try:
            # Phase 1: Scraping Letterboxd watchlist
            yield f"data: {json.dumps({'phase': 'scraping', 'message': 'Fetching watchlist from Letterboxd...'})}\n\n"

            films = await letterboxd_sync.scrape_watchlist(username)

            total = len(films)
            if not films:
                yield f"data: {json.dumps({'phase': 'done', 'imported': 0, 'skipped': 0, 'failed': 0, 'total_found': 0})}\n\n"
                return

            yield f"data: {json.dumps({'phase': 'importing', 'message': f'Found {total} films in watchlist. Starting import...', 'total': total, 'current': 0})}\n\n"

            # Phase 2: Import with progress
            entries = [
                LetterboxdEntry(
                    name=film.title,
                    year=film.year,
                    rating=None,
                    watched_date=None,
                    letterboxd_uri=film.letterboxd_uri,
                    rewatch=False,
                )
                for film in films
            ]

            imported = 0
            skipped = 0
            failed = 0
            errors: list[str] = []

            async with async_session_maker() as db:
                for i, entry in enumerate(entries):
                    # Check if client disconnected
                    if await request.is_disconnected():
                        return

                    status, error_msg = await letterboxd_importer.import_single_entry(
                        db=db,
                        user_id=user_id,
                        entry=entry,
                        skip_existing=skip_existing,
                        fetch_metadata=fetch_metadata,
                        force_update=False,  # Watchlist items don't have ratings
                    )

                    if status in ("imported", "updated"):
                        imported += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        if error_msg:
                            errors.append(error_msg)

                    # Send progress every film
                    progress_data = {
                        "phase": "importing",
                        "current": i + 1,
                        "total": total,
                        "imported": imported,
                        "skipped": skipped,
                        "failed": failed,
                        "current_film": entry.name,
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                    # Small delay to prevent overwhelming
                    await asyncio.sleep(0.01)

                # Final commit
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()

            # Invalidate search cache
            if imported > 0:
                invalidate_user_search_cache(user_id)

            # Final result with errors
            yield f"data: {json.dumps({'phase': 'done', 'imported': imported, 'skipped': skipped, 'failed': failed, 'total_found': total, 'errors': errors[:20]})}\n\n"

        except Exception as e:
            logger.exception("Error during Letterboxd watchlist sync stream")
            yield f"data: {json.dumps({'phase': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== FRIENDS RATINGS ====================


class FriendRatingResponse(BaseModel):
    """Friend rating response."""

    username: str
    rating: float | None
    liked: bool
    review_exists: bool


class FilmFriendsRatingsResponse(BaseModel):
    """Response with friends' ratings for a film."""

    film_slug: str
    ratings: list[FriendRatingResponse]
    average_rating: float | None


@router.get("/letterboxd/friends-ratings/{film_slug}", response_model=FilmFriendsRatingsResponse)
async def get_friends_ratings(
    film_slug: str,
    user: Annotated[User, Depends(get_current_user)],
) -> FilmFriendsRatingsResponse:
    """Get ratings from friends for a specific film.

    Checks each friend's Letterboxd profile directly for their rating.

    Args:
        film_slug: The Letterboxd film slug (e.g., 'dune-part-two')
    """
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    # Get friends' ratings using the direct method (checks each friend's page)
    ratings = await letterboxd_sync.get_friends_ratings_direct(
        film_slug=film_slug,
        username=username,
    )

    # Calculate average
    rated_values = [r.rating for r in ratings if r.rating is not None]
    average_rating = sum(rated_values) / len(rated_values) if rated_values else None

    return FilmFriendsRatingsResponse(
        film_slug=film_slug,
        ratings=[
            FriendRatingResponse(
                username=r.username,
                rating=r.rating,
                liked=r.liked,
                review_exists=r.review_exists,
            )
            for r in ratings
        ],
        average_rating=round(average_rating, 2) if average_rating else None,
    )


class FollowingResponse(BaseModel):
    """Response with list of following usernames."""

    usernames: list[str]
    count: int


@router.get("/letterboxd/following", response_model=FollowingResponse)
async def get_letterboxd_following(
    user: Annotated[User, Depends(get_current_user)],
) -> FollowingResponse:
    """Get list of users the authenticated user follows on Letterboxd."""
    username = user.letterboxd_username

    if not username:
        raise HTTPException(
            status_code=400,
            detail="Letterboxd username not configured. Set it in settings first.",
        )

    following = await letterboxd_sync.get_following(username)

    return FollowingResponse(
        usernames=following,
        count=len(following),
    )


# ==================== NOTION IMPORT ====================


@router.post("/notion", response_model=ImportResponse)
async def import_notion(
    file: Annotated[UploadFile, File(description="Notion CSV export")],
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    skip_existing: bool = True,
    fetch_metadata: bool = True,
) -> ImportResponse:
    """Import media from Notion CSV export.

    Supports various media types:
    - Film → Film
    - Livre/Book → Book
    - TV Series/Série → Series
    - Discussion → Podcast
    - Reportage → YouTube video

    Args:
        file: CSV file upload
        skip_existing: Skip media already in library (default: True)
        fetch_metadata: Fetch full metadata from external APIs (default: True)
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    # Read file content
    content = await file.read()
    try:
        csv_content = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            csv_content = content.decode("latin-1")
        except UnicodeDecodeError:
            csv_content = content.decode("utf-8", errors="ignore")

    # Parse CSV
    entries = notion_importer.parse_csv(csv_content)

    if not entries:
        raise HTTPException(status_code=400, detail="No valid entries found in CSV")

    # Import entries one by one
    imported = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for entry in entries:
        status, error_msg = await notion_importer.import_single_entry(
            db=db,
            user_id=user.id,
            entry=entry,
            skip_existing=skip_existing,
            fetch_metadata=fetch_metadata,
        )

        if status == "imported":
            imported += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
            if error_msg:
                errors.append(error_msg)

    # Final commit
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    # Invalidate search cache
    if imported > 0:
        invalidate_user_search_cache(user.id)

    return ImportResponse(
        imported=imported,
        skipped=skipped,
        failed=failed,
        errors=errors[:10] if errors else None,
    )


@router.get("/notion/import-stream")
async def import_notion_stream(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
):
    """Placeholder for SSE stream - actual upload happens via POST first."""
    raise HTTPException(
        status_code=400,
        detail="Use POST /api/import/notion-stream with file upload",
    )


@router.post("/notion-stream")
async def import_notion_stream_post(
    request: Request,
    file: Annotated[UploadFile, File(description="Notion CSV export")],
    user: Annotated[User, Depends(get_current_user)],
    skip_existing: bool = True,
    fetch_metadata: bool = True,
):
    """Import media from Notion CSV with progress streaming via SSE.

    Returns Server-Sent Events with progress updates.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    # Read file content
    content = await file.read()
    try:
        csv_content = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            csv_content = content.decode("latin-1")
        except UnicodeDecodeError:
            csv_content = content.decode("utf-8", errors="ignore")

    # Parse CSV
    entries = notion_importer.parse_csv(csv_content)

    if not entries:
        raise HTTPException(status_code=400, detail="No valid entries found in CSV")

    # Capture user data
    user_id = user.id

    async def generate():
        """Generate SSE events for progress."""
        from src.db import async_session_maker

        try:
            total = len(entries)
            yield f"data: {json.dumps({'phase': 'parsing', 'message': f'Found {total} entries in CSV. Starting import...', 'total': total, 'current': 0})}\n\n"

            imported = 0
            skipped = 0
            failed = 0
            errors: list[str] = []

            async with async_session_maker() as db:
                for i, entry in enumerate(entries):
                    # Check if client disconnected
                    if await request.is_disconnected():
                        return

                    status, error_msg = await notion_importer.import_single_entry(
                        db=db,
                        user_id=user_id,
                        entry=entry,
                        skip_existing=skip_existing,
                        fetch_metadata=fetch_metadata,
                    )

                    if status == "imported":
                        imported += 1
                    elif status == "skipped":
                        skipped += 1
                        # Collect skip reasons for debugging
                        if error_msg:
                            errors.append(error_msg)
                    else:
                        failed += 1
                        if error_msg:
                            errors.append(error_msg)

                    # Send progress
                    progress_data = {
                        "phase": "importing",
                        "current": i + 1,
                        "total": total,
                        "imported": imported,
                        "skipped": skipped,
                        "failed": failed,
                        "current_item": entry.name,
                        "current_type": entry.type or "unknown",
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                    # Small delay to prevent overwhelming
                    await asyncio.sleep(0.01)

                # Final commit
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()

            # Invalidate search cache
            if imported > 0:
                invalidate_user_search_cache(user_id)

            # Collect unique skip reasons for summary
            skip_reasons = {}
            for err in errors:
                if "unsupported type" in err.lower():
                    # Extract type from error message
                    match = re.search(r"unsupported type '([^']*)'", err.lower())
                    if match:
                        t = match.group(1)
                        skip_reasons[t] = skip_reasons.get(t, 0) + 1

            # Final result
            result_data = {
                'phase': 'done',
                'imported': imported,
                'skipped': skipped,
                'failed': failed,
                'total': total,
                'errors': errors[:20],
            }
            if skip_reasons:
                result_data['skip_summary'] = skip_reasons
            yield f"data: {json.dumps(result_data)}\n\n"

        except Exception as e:
            logger.exception("Error during Notion import stream")
            yield f"data: {json.dumps({'phase': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
