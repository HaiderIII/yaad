"""Letterboxd background sync service."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import User
from src.services.imports.letterboxd import LetterboxdEntry, letterboxd_importer
from src.services.imports.letterboxd_sync import letterboxd_sync

logger = logging.getLogger(__name__)


async def sync_user_letterboxd(db: AsyncSession, user: User, full_import: bool = False) -> dict:
    """Sync Letterboxd for a single user.

    Args:
        db: Database session
        user: User to sync
        full_import: If True, scrape all films. If False, use RSS (last ~50).

    Returns:
        dict with imported/skipped/failed counts
    """
    username = user.letterboxd_username
    if not username:
        return {"imported": 0, "skipped": True, "reason": "not_configured"}

    try:
        # Fetch films from Letterboxd
        if full_import:
            films = await letterboxd_sync.scrape_all_films(username, include_ratings=True)
            sync_type = "full"
        else:
            films = await letterboxd_sync.fetch_rss(username)
            sync_type = "rss"

        if not films:
            return {"imported": 0, "skipped": 0, "failed": 0, "sync_type": sync_type}

        # Convert to LetterboxdEntry format
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
            user_id=user.id,
            entries=entries,
            skip_existing=True,  # Always skip existing for background sync
            fetch_metadata=True,
        )

        # Invalidate search cache if any imports
        if result.imported > 0:
            from src.api.search import invalidate_user_search_cache
            invalidate_user_search_cache(user.id)

        return {
            "imported": result.imported,
            "skipped": result.skipped,
            "failed": result.failed,
            "total_found": len(films),
            "sync_type": sync_type,
        }

    except Exception as e:
        logger.error(f"Letterboxd sync failed for user {user.id}: {e}")
        return {"imported": 0, "skipped": 0, "failed": 0, "error": str(e)}


async def sync_letterboxd_for_user_id(user_id: int, full_import: bool = False) -> dict:
    """Sync Letterboxd for a user by ID (for background tasks)."""
    from src.db import async_session_maker

    async with async_session_maker() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            return {"error": "User not found"}

        return await sync_user_letterboxd(db, user, full_import)


async def sync_all_letterboxd_users(full_import: bool = False) -> dict:
    """Sync Letterboxd for all configured users (for periodic job).

    Args:
        full_import: If True, do full scrape. If False, use RSS only.
    """
    from src.db import async_session_maker

    async with async_session_maker() as db:
        # Get all users with Letterboxd configured
        result = await db.execute(
            select(User).where(User.letterboxd_username.isnot(None))
        )
        users = result.scalars().all()

        total_imported = 0
        total_skipped = 0
        total_failed = 0
        users_processed = 0

        for user in users:
            try:
                result = await sync_user_letterboxd(db, user, full_import)
                if not result.get("skipped"):
                    total_imported += result.get("imported", 0)
                    total_skipped += result.get("skipped", 0)
                    total_failed += result.get("failed", 0)
                    users_processed += 1
            except Exception as e:
                logger.error(f"Failed to sync Letterboxd for user {user.id}: {e}")

        logger.info(
            f"Letterboxd sync completed: {users_processed} users, "
            f"{total_imported} imported, {total_skipped} skipped"
        )

        return {
            "users_processed": users_processed,
            "total_imported": total_imported,
            "total_skipped": total_skipped,
            "total_failed": total_failed,
        }
