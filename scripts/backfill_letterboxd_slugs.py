#!/usr/bin/env python3
"""Backfill letterboxd_slug for existing films imported from Letterboxd.

This script fetches the user's Letterboxd films and matches them with
existing media entries to populate the letterboxd_slug field.

Usage:
    python scripts/backfill_letterboxd_slugs.py
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import async_session_maker, init_db
from src.models.media import Media, MediaType
from src.models.user import User
from src.services.imports.letterboxd_sync import letterboxd_sync

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


async def backfill_user_slugs(db: AsyncSession, user: User) -> dict:
    """Backfill letterboxd_slug for a user's films."""
    username = user.letterboxd_username
    if not username:
        return {"user_id": user.id, "skipped": True, "reason": "no_letterboxd_username"}

    logger.info(f"Processing user {user.id} ({username})...")

    # Get user's films without letterboxd_slug
    result = await db.execute(
        select(Media).where(
            Media.user_id == user.id,
            Media.type == MediaType.FILM,
            Media.letterboxd_slug.is_(None),
        )
    )
    films_without_slug = result.scalars().all()

    if not films_without_slug:
        logger.info(f"  No films need slug backfill")
        return {"user_id": user.id, "updated": 0, "total": 0}

    logger.info(f"  Found {len(films_without_slug)} films without slug")

    # Fetch all films from Letterboxd
    try:
        letterboxd_films = await letterboxd_sync.scrape_all_films(username, include_ratings=True)
    except Exception as e:
        logger.error(f"  Failed to fetch Letterboxd films: {e}")
        return {"user_id": user.id, "error": str(e)}

    if not letterboxd_films:
        logger.info(f"  No films found on Letterboxd")
        return {"user_id": user.id, "updated": 0, "total": len(films_without_slug)}

    # Create lookup by (title_lower, year) -> slug
    letterboxd_lookup: dict[tuple[str, int | None], str] = {}
    for film in letterboxd_films:
        if film.letterboxd_uri:
            # Extract slug from URI
            import re
            match = re.search(r"/film/([^/]+)/?", film.letterboxd_uri)
            if match:
                slug = match.group(1)
                key = (film.title.lower(), film.year)
                letterboxd_lookup[key] = slug

    logger.info(f"  Built lookup with {len(letterboxd_lookup)} Letterboxd entries")

    # Match and update
    updated = 0
    for media in films_without_slug:
        # Try exact match first
        key = (media.title.lower(), media.year)
        slug = letterboxd_lookup.get(key)

        # Try with original_title if no match
        if not slug and media.original_title:
            key = (media.original_title.lower(), media.year)
            slug = letterboxd_lookup.get(key)

        # Try without year
        if not slug:
            for (title, year), s in letterboxd_lookup.items():
                if title == media.title.lower():
                    slug = s
                    break

        if slug:
            media.letterboxd_slug = slug
            updated += 1
            logger.info(f"    Matched: {media.title} -> {slug}")

    await db.commit()
    logger.info(f"  Updated {updated}/{len(films_without_slug)} films")

    return {
        "user_id": user.id,
        "username": username,
        "updated": updated,
        "total": len(films_without_slug),
    }


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("Letterboxd Slug Backfill Script")
    logger.info("=" * 60)

    await init_db()

    async with async_session_maker() as db:
        # Get all users with Letterboxd configured
        result = await db.execute(
            select(User).where(User.letterboxd_username.isnot(None))
        )
        users = result.scalars().all()

        if not users:
            logger.info("No users with Letterboxd configured")
            return

        logger.info(f"Found {len(users)} users with Letterboxd configured\n")

        total_updated = 0
        for user in users:
            result = await backfill_user_slugs(db, user)
            total_updated += result.get("updated", 0)
            print()

        logger.info("=" * 60)
        logger.info(f"Total films updated: {total_updated}")
        logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
