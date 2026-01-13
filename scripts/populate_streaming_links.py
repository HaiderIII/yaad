#!/usr/bin/env python3
"""Populate streaming_links for all films and series.

This script fetches streaming availability from JustWatch for all media
that don't have streaming_links cached yet or have outdated links.

Usage:
    python scripts/populate_streaming_links.py [--all] [--user-id=ID] [--days=N]

Options:
    --all       Refresh all media, even those with existing links
    --user-id   Only process media for a specific user
    --days      Refresh links older than N days (default: 1)
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, update

from src.db.database import async_session_maker
from src.models.media import Media, MediaType
from src.models.user import User
from src.services.metadata.justwatch import justwatch_service


async def populate_streaming_links(
    refresh_all: bool = False,
    user_id: int | None = None,
    max_age_days: int = 1,
) -> None:
    """Populate streaming_links for all films and series."""
    async with async_session_maker() as db:
        # Build query
        query = select(Media).where(Media.type.in_([MediaType.FILM, MediaType.SERIES]))

        if user_id:
            query = query.where(Media.user_id == user_id)

        if not refresh_all:
            # Only get media without streaming_links or with old links
            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            query = query.where(
                (Media.streaming_links.is_(None))
                | (Media.streaming_links_updated.is_(None))
                | (Media.streaming_links_updated < cutoff)
            )

        # Get user countries for lookups
        users_result = await db.execute(select(User.id, User.country))
        user_countries = {row.id: row.country for row in users_result.all()}

        result = await db.execute(query)
        media_list = result.scalars().all()

        print(f"Found {len(media_list)} media to process")

        success_count = 0
        error_count = 0
        skip_count = 0

        for i, media in enumerate(media_list, 1):
            if not media.external_id:
                print(f"[{i}/{len(media_list)}] {media.title[:40]:40} - No external_id, skipping")
                skip_count += 1
                continue

            country = user_countries.get(media.user_id, "FR")
            media_type = "movie" if media.type == MediaType.FILM else "tv"

            try:
                tmdb_id = int(media.external_id)
                result = await justwatch_service.get_streaming_links(
                    tmdb_id,
                    media_type=media_type,
                    country=country,
                    title=media.title,
                    year=media.year,
                )

                if result and result.get("links"):
                    media.streaming_links = result["links"]
                    media.streaming_links_updated = datetime.now(UTC)
                    await db.commit()
                    link_count = len(result["links"])
                    print(f"[{i}/{len(media_list)}] {media.title[:40]:40} - {link_count} providers found")
                    success_count += 1
                else:
                    print(f"[{i}/{len(media_list)}] {media.title[:40]:40} - No providers found")
                    # Still mark as updated to avoid re-fetching
                    media.streaming_links = {}
                    media.streaming_links_updated = datetime.now(UTC)
                    await db.commit()
                    skip_count += 1

                # Rate limiting - be nice to JustWatch API
                await asyncio.sleep(0.5)

            except Exception as e:
                print(f"[{i}/{len(media_list)}] {media.title[:40]:40} - Error: {e}")
                error_count += 1

        print(f"\nDone! Success: {success_count}, Skipped: {skip_count}, Errors: {error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate streaming links for media")
    parser.add_argument("--all", action="store_true", help="Refresh all media, even with existing links")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    parser.add_argument("--days", type=int, default=1, help="Refresh links older than N days (default: 1)")
    args = parser.parse_args()

    asyncio.run(populate_streaming_links(
        refresh_all=args.all,
        user_id=args.user_id,
        max_age_days=args.days,
    ))
