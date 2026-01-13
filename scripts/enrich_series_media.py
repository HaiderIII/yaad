#!/usr/bin/env python3
"""Enrich incomplete Series entries with TMDB metadata.

This script fetches metadata from TMDB for media entries that:
- Are of type SERIES
- Are missing metadata (title, description, cover, year, etc.)

Usage:
    python scripts/enrich_series_media.py [--all] [--user-id=ID] [--dry-run] [--force]

Options:
    --all       Process all series, even those with metadata
    --user-id   Only process media for a specific user
    --dry-run   Show what would be updated without making changes
    --force     Overwrite existing data with TMDB metadata (not just fill empty fields)
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.db.crud import get_or_create_author, get_or_create_genre
from src.db.database import async_session_maker
from src.models.media import Media, MediaType
from src.services.metadata.tmdb import tmdb_service


def is_incomplete(media: Media) -> bool:
    """Check if media is missing important metadata."""
    # Missing cover
    if not media.cover_url:
        return True
    # Missing description
    if not media.description:
        return True
    # Missing year
    if not media.year:
        return True
    # Title looks like a placeholder or is very short
    if len(media.title) < 3:
        return True
    # Missing external_id (TMDB ID)
    if not media.external_id:
        return True
    return False


def normalize_title(title: str) -> str:
    """Normalize title for better search matching."""
    import re
    # Remove common suffixes like (2024), - Season 1, etc.
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
    title = re.sub(r'\s*-\s*Season\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*-\s*Saison\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*S\d+\s*$', '', title)
    return title.strip()


async def enrich_series_media(
    process_all: bool = False,
    user_id: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Enrich Series entries with TMDB metadata.

    Args:
        process_all: Process all series, not just incomplete
        user_id: Only process media for a specific user
        dry_run: Show what would be updated without making changes
        force: Overwrite existing data with TMDB metadata
    """
    async with async_session_maker() as db:
        # Build query - find media of type SERIES
        query = select(Media).where(Media.type == MediaType.SERIES)

        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        media_list = result.scalars().all()

        print(f"Found {len(media_list)} Series entries")

        # Build a set of existing (user_id, type, external_id) to avoid duplicates
        existing_external_ids: dict[tuple[int, MediaType, str], int] = {}
        for m in media_list:
            if m.external_id:
                key = (m.user_id, m.type, m.external_id)
                existing_external_ids[key] = m.id

        # Filter to incomplete ones (unless --all or --force)
        if not process_all and not force:
            media_list = [m for m in media_list if is_incomplete(m)]
            print(f"Filtered to {len(media_list)} incomplete entries")

        if force:
            print("FORCE MODE: Will overwrite existing metadata with TMDB data")

        if not media_list:
            print("No media to process")
            return

        updated_count = 0
        error_count = 0
        skipped_count = 0
        not_found_count = 0
        duplicate_count = 0

        for i, media in enumerate(media_list, 1):
            print(f"[{i}/{len(media_list)}] {media.title[:50]:50}", end=" ")

            try:
                # If we have an external_id (TMDB ID), fetch directly
                if media.external_id and media.external_id.isdigit():
                    info = await tmdb_service.get_tv_details(int(media.external_id))
                    if info:
                        print(f"(ID: {media.external_id})", end=" ")
                    else:
                        # ID didn't work, try search
                        info = None
                else:
                    info = None

                # If no info yet, search by title
                if not info:
                    search_title = normalize_title(media.title)
                    search_results = await tmdb_service.search_tv(
                        search_title,
                        year=media.year if media.year else None
                    )

                    if not search_results:
                        # Try without year
                        search_results = await tmdb_service.search_tv(search_title)

                    if not search_results:
                        print("- Not found on TMDB")
                        not_found_count += 1
                        continue

                    # Use first result
                    best_match = search_results[0]
                    tmdb_id = best_match["id"]

                    # Get full details
                    info = await tmdb_service.get_tv_details(tmdb_id)

                    if not info:
                        print("- Could not fetch details")
                        error_count += 1
                        continue

                    print(f"(found: {info.get('title', '')[:30]})", end=" ")

                # Track what we're updating
                updates = []

                # Cover
                if info.get("cover_url") and (force or not media.cover_url):
                    if not dry_run:
                        media.cover_url = info["cover_url"]
                    updates.append("cover")

                # Description
                if info.get("description") and (force or not media.description):
                    desc = info["description"][:2000]  # Limit length
                    if not dry_run:
                        media.description = desc
                    updates.append("description")

                # Year
                if info.get("year") and (force or not media.year):
                    year = int(info["year"]) if info["year"] else None
                    if year and not dry_run:
                        media.year = year
                    if year:
                        updates.append(f"year={year}")

                # Duration (average episode runtime)
                if info.get("duration_minutes") and (force or not media.duration_minutes):
                    if not dry_run:
                        media.duration_minutes = info["duration_minutes"]
                    updates.append(f"duration={info['duration_minutes']}min")

                # External ID - check for duplicates before updating
                new_external_id = str(info["id"])
                if force or not media.external_id:
                    new_key = (media.user_id, media.type, new_external_id)
                    existing_media_id = existing_external_ids.get(new_key)
                    # Only update if no other media has this external_id, or if it's this same media
                    if existing_media_id is None or existing_media_id == media.id:
                        if not dry_run:
                            media.external_id = new_external_id
                            existing_external_ids[new_key] = media.id
                        updates.append("external_id")
                    else:
                        updates.append("external_id(skipped-duplicate)")
                        duplicate_count += 1

                # External URL
                if info.get("external_url") and (force or not media.external_url):
                    if not dry_run:
                        media.external_url = info["external_url"]
                    updates.append("url")

                # Title - in force mode always update, otherwise only if placeholder
                if info.get("title"):
                    if force or len(media.title) < 3:
                        if not dry_run:
                            media.title = info["title"]
                        updates.append("title")

                # Genres
                if info.get("genres") and force:
                    if not dry_run:
                        try:
                            genre_objects = []
                            for genre_name in info["genres"][:5]:  # Limit to 5 genres
                                genre = await get_or_create_genre(db, genre_name, MediaType.SERIES)
                                genre_objects.append(genre)
                            media.genres = genre_objects
                        except Exception:
                            pass  # Skip genre update on error
                    updates.append(f"genres={len(info['genres'])}")

                # Creators/Directors
                if info.get("directors") and force:
                    if not dry_run:
                        try:
                            author_objects = []
                            for director in info["directors"][:3]:  # Limit to 3 creators
                                author = await get_or_create_author(db, director["name"], MediaType.SERIES)
                                author_objects.append(author)
                            media.authors = author_objects
                        except Exception:
                            pass  # Skip author update on error
                    if info["directors"]:
                        updates.append(f"creators={info['directors'][0]['name'][:15]}")

                if updates:
                    print(f"- Updated: {', '.join(updates)}")
                    updated_count += 1
                else:
                    print("- Already complete")
                    skipped_count += 1

                # Rate limiting
                await asyncio.sleep(0.3)

            except Exception as e:
                print(f"- Error: {e}")
                error_count += 1

        if not dry_run:
            await db.commit()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Done!")
        print(f"  Updated: {updated_count}")
        print(f"  Skipped: {skipped_count}")
        print(f"  Not found: {not_found_count}")
        print(f"  Errors:  {error_count}")
        if duplicate_count:
            print(f"  Duplicates skipped: {duplicate_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich Series with TMDB metadata")
    parser.add_argument("--all", action="store_true", help="Process all series, not just incomplete")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without changes")
    parser.add_argument("--force", action="store_true", help="Overwrite existing data with TMDB metadata")
    args = parser.parse_args()

    asyncio.run(enrich_series_media(
        process_all=args.all,
        user_id=args.user_id,
        dry_run=args.dry_run,
        force=args.force,
    ))
