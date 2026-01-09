#!/usr/bin/env python3
"""Fix TV series that were imported from Letterboxd as films.

This script checks films that have incomplete metadata (likely TV series)
and re-checks them against TMDB to see if they're actually TV series.

Usage:
    python scripts/fix_letterboxd_series.py
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
from src.models.media import Media, MediaStatus, MediaType
from src.models.user import User
from src.services.metadata.tmdb import tmdb_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def score_match(title: str, year: int | None, result: dict) -> float:
    """Score a TMDB result based on title similarity and year match."""
    title_lower = title.lower().strip()
    score = 0.0
    result_title = (result.get("title") or result.get("original_title") or "").lower()
    result_local = (result.get("local_title") or "").lower()

    # Exact title match
    if result_title == title_lower or result_local == title_lower:
        score += 100
    elif title_lower in result_title or title_lower in result_local:
        score += 50
    elif result_title in title_lower or result_local in title_lower:
        score += 30

    # Year match
    result_year = result.get("year")
    if result_year and year:
        try:
            if int(result_year) == year:
                score += 50
            elif abs(int(result_year) - year) <= 1:
                score += 20
        except (ValueError, TypeError):
            pass

    # Popularity bonus
    vote = result.get("vote_average", 0) or 0
    score += vote * 2

    return score


async def check_and_fix_media(db: AsyncSession, media: Media) -> str:
    """Check if a film is actually a TV series and fix it.

    Returns:
        'converted' if converted to series
        'duplicate' if series already exists (delete the film)
        'kept' if kept as film
    """
    # Search both movies and TV
    movie_results = await tmdb_service.search_movies(media.title, year=media.year)
    tv_results = await tmdb_service.search_tv(media.title, year=media.year)

    # Score results
    best_movie = movie_results[0] if movie_results else None
    best_tv = tv_results[0] if tv_results else None

    movie_score = score_match(media.title, media.year, best_movie) if best_movie else -1
    tv_score = score_match(media.title, media.year, best_tv) if best_tv else -1

    # If TV score is significantly better, it's likely a TV series
    if tv_score > movie_score + 20 and best_tv:
        tmdb_id = best_tv["id"]

        # Check if this series already exists for this user
        existing = await db.execute(
            select(Media).where(
                Media.user_id == media.user_id,
                Media.type == MediaType.SERIES,
                Media.external_id == str(tmdb_id),
            )
        )
        if existing.scalar_one_or_none():
            # Series already exists - delete the duplicate film entry
            logger.info(f"  Deleting duplicate: {media.title} (series already exists)")
            await db.delete(media)
            return "duplicate"

        details = await tmdb_service.get_tv_details(tmdb_id)

        if details:
            logger.info(f"  Converting to TV series: {media.title}")
            logger.info(f"    Movie score: {movie_score:.1f}, TV score: {tv_score:.1f}")

            # Update the media entry
            media.type = MediaType.SERIES
            media.external_id = str(tmdb_id)
            media.title = details.get("title") or media.title
            media.original_title = details.get("original_title")
            media.description = details.get("description")
            media.cover_url = details.get("cover_url")
            media.year = int(details.get("year")) if details.get("year") else media.year
            media.tmdb_rating = details.get("tmdb_rating")
            media.tmdb_vote_count = details.get("tmdb_vote_count")
            media.popularity = details.get("popularity")
            media.original_language = details.get("original_language")
            media.tagline = details.get("tagline")
            media.cast = details.get("cast")
            media.keywords = details.get("keywords")
            media.certification = details.get("certification")
            media.number_of_seasons = details.get("number_of_seasons")
            media.number_of_episodes = details.get("number_of_episodes")
            media.series_status = details.get("series_status")
            media.networks = details.get("networks")

            # Clear film-specific fields
            media.duration_minutes = None
            media.budget = None
            media.revenue = None
            media.production_countries = None
            media.collection_id = None
            media.collection_name = None

            return "converted"

    return "kept"


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("Fix Letterboxd Series Script")
    logger.info("=" * 60)

    await init_db()

    async with async_session_maker() as db:
        # Find films that look like they might be TV series
        # (incomplete metadata, especially missing duration)
        result = await db.execute(
            select(Media).where(
                Media.type == MediaType.FILM,
                # Films without duration are suspicious
                Media.duration_minutes.is_(None),
            )
        )
        suspicious_films = result.scalars().all()

        if not suspicious_films:
            logger.info("No suspicious films found")
            return

        logger.info(f"Found {len(suspicious_films)} films without duration to check\n")

        converted = 0
        deleted = 0
        for media in suspicious_films:
            logger.info(f"Checking: {media.title} ({media.year})")
            result = await check_and_fix_media(db, media)
            if result == "converted":
                converted += 1
            elif result == "duplicate":
                deleted += 1

        await db.commit()

        logger.info("\n" + "=" * 60)
        logger.info(f"Converted {converted} films to TV series")
        logger.info(f"Deleted {deleted} duplicate entries")
        logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
