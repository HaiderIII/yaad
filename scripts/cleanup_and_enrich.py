#!/usr/bin/env python3
"""Cleanup and enrich media entries.

This script:
1. Removes Podcasts and YouTube videos without URLs
2. Enriches all Films with TMDB metadata (cover, description, year, directors, genres)
3. Enriches all Series with TMDB metadata (cover, description, year, creators, genres)

Usage:
    python scripts/cleanup_and_enrich.py [--dry-run] [--user-id=ID]

Options:
    --dry-run   Show what would be done without making changes
    --user-id   Only process media for a specific user
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


def normalize_title(title: str) -> str:
    """Normalize title for better search matching."""
    import re
    # Remove common suffixes like (2024), - Season 1, etc.
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
    title = re.sub(r'\s*-\s*Season\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*-\s*Saison\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*S\d+\s*$', '', title)
    return title.strip()


async def enrich_media_type(
    db,
    media_type: MediaType,
    existing_external_ids: dict,
    dry_run: bool,
    user_id: int | None,
) -> tuple[int, int, list, list]:
    """Enrich a specific media type with TMDB metadata.

    Returns: (updated_count, already_complete, not_found_list, errors_list)
    """
    type_name = "Films" if media_type == MediaType.FILM else "Series"
    author_label = "directors" if media_type == MediaType.FILM else "creators"

    # Get all media of this type
    query = select(Media).where(Media.type == media_type)
    if user_id:
        query = query.where(Media.user_id == user_id)

    result = await db.execute(query)
    media_list = result.scalars().all()

    print(f"Found {len(media_list)} {type_name} entries to check\n")

    # Track results
    updated_count = 0
    already_complete = 0
    not_found = []
    errors = []

    for i, media in enumerate(media_list, 1):
        # Check what's missing
        missing = []
        if not media.cover_url:
            missing.append("cover")
        if not media.description:
            missing.append("description")
        if not media.year:
            missing.append("year")
        if not media.external_id:
            missing.append("external_id")
        if not media.authors or len(media.authors) == 0:
            missing.append(author_label)
        if not media.genres or len(media.genres) == 0:
            missing.append("genres")

        # If nothing missing, skip
        if not missing:
            already_complete += 1
            continue

        print(f"[{i}/{len(media_list)}] {media.title[:45]:45} missing: {', '.join(missing)}", end=" ")

        try:
            info = None

            # If we have an external_id (TMDB ID), fetch directly
            if media.external_id and media.external_id.isdigit():
                if media_type == MediaType.FILM:
                    info = await tmdb_service.get_movie_details(int(media.external_id))
                else:
                    info = await tmdb_service.get_tv_details(int(media.external_id))

            # If no info yet, search by title
            if not info:
                search_title = normalize_title(media.title)

                if media_type == MediaType.FILM:
                    search_results = await tmdb_service.search_movies(
                        search_title,
                        year=media.year if media.year else None
                    )
                else:
                    search_results = await tmdb_service.search_tv(
                        search_title,
                        year=media.year if media.year else None
                    )

                if not search_results:
                    # Try without year
                    if media_type == MediaType.FILM:
                        search_results = await tmdb_service.search_movies(search_title)
                    else:
                        search_results = await tmdb_service.search_tv(search_title)

                if not search_results:
                    print("- NOT FOUND")
                    not_found.append(f"{media.title} (missing: {', '.join(missing)})")
                    continue

                # Use first result
                best_match = search_results[0]

                if media_type == MediaType.FILM:
                    info = await tmdb_service.get_movie_details(best_match["id"])
                else:
                    info = await tmdb_service.get_tv_details(best_match["id"])

                if not info:
                    print("- Could not fetch details")
                    errors.append(f"{media.title}: Could not fetch TMDB details")
                    continue

            # Track what we're updating
            updates = []

            # Cover
            if "cover" in missing and info.get("cover_url"):
                if not dry_run:
                    media.cover_url = info["cover_url"]
                updates.append("cover")

            # Description
            if "description" in missing and info.get("description"):
                if not dry_run:
                    media.description = info["description"][:2000]
                updates.append("description")

            # Year
            if "year" in missing and info.get("year"):
                year = int(info["year"]) if info["year"] else None
                if year and not dry_run:
                    media.year = year
                if year:
                    updates.append(f"year={year}")

            # Duration
            if not media.duration_minutes and info.get("duration_minutes"):
                if not dry_run:
                    media.duration_minutes = info["duration_minutes"]
                updates.append(f"duration={info['duration_minutes']}min")

            # External ID
            if "external_id" in missing:
                new_external_id = str(info["id"])
                new_key = (media.user_id, media.type, new_external_id)
                existing_media_id = existing_external_ids.get(new_key)
                if existing_media_id is None or existing_media_id == media.id:
                    if not dry_run:
                        media.external_id = new_external_id
                        existing_external_ids[new_key] = media.id
                    updates.append("external_id")

            # Directors/Creators
            if author_label in missing and info.get("directors"):
                if not dry_run:
                    try:
                        author_objects = []
                        for director in info["directors"][:3]:
                            author = await get_or_create_author(db, director["name"], media_type)
                            author_objects.append(author)
                        if author_objects:
                            media.authors = author_objects
                    except Exception as e:
                        errors.append(f"{media.title}: Failed to add {author_label} - {e}")
                if info["directors"]:
                    names = ", ".join(d["name"] for d in info["directors"][:2])
                    updates.append(f"{author_label}={names[:25]}")

            # Genres
            if "genres" in missing and info.get("genres"):
                if not dry_run:
                    try:
                        genre_objects = []
                        for genre_name in info["genres"][:5]:
                            genre = await get_or_create_genre(db, genre_name, media_type)
                            genre_objects.append(genre)
                        if genre_objects:
                            media.genres = genre_objects
                    except Exception as e:
                        errors.append(f"{media.title}: Failed to add genres - {e}")
                updates.append(f"genres={len(info['genres'])}")

            if updates:
                print(f"- Updated: {', '.join(updates)}")
                updated_count += 1
            else:
                print("- No updates needed")

            # Rate limiting
            await asyncio.sleep(0.25)

        except Exception as e:
            print(f"- ERROR: {e}")
            errors.append(f"{media.title}: {str(e)}")

    return updated_count, already_complete, not_found, errors


async def cleanup_and_enrich(
    dry_run: bool = False,
    user_id: int | None = None,
) -> None:
    """Cleanup and enrich media entries."""

    async with async_session_maker() as db:
        # ========================================
        # PART 1: Remove Podcasts/Videos without URL
        # ========================================
        print("=" * 60)
        print("PART 1: Removing Podcasts and Videos without URL")
        print("=" * 60)

        # Find podcasts and videos without external_url
        query = select(Media).where(
            Media.type.in_([MediaType.PODCAST, MediaType.YOUTUBE]),
            (Media.external_url.is_(None)) | (Media.external_url == "")
        )
        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        media_to_delete = result.scalars().all()

        print(f"Found {len(media_to_delete)} entries without URL to delete:")

        deleted_podcasts = 0
        deleted_videos = 0

        for media in media_to_delete:
            type_str = "Podcast" if media.type == MediaType.PODCAST else "Video"
            print(f"  - [{type_str}] {media.title[:60]}")
            if media.type == MediaType.PODCAST:
                deleted_podcasts += 1
            else:
                deleted_videos += 1

        if not dry_run and media_to_delete:
            for media in media_to_delete:
                await db.delete(media)
            await db.commit()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Deleted: {deleted_podcasts} podcasts, {deleted_videos} videos")

        # Build external_ids index for all films and series
        query = select(Media).where(Media.type.in_([MediaType.FILM, MediaType.SERIES]))
        if user_id:
            query = query.where(Media.user_id == user_id)
        result = await db.execute(query)
        all_media = result.scalars().all()

        existing_external_ids: dict[tuple[int, MediaType, str], int] = {}
        for m in all_media:
            if m.external_id:
                key = (m.user_id, m.type, m.external_id)
                existing_external_ids[key] = m.id

        # ========================================
        # PART 2: Enrich Films with TMDB
        # ========================================
        print("\n" + "=" * 60)
        print("PART 2: Enriching Films with TMDB metadata")
        print("=" * 60)

        films_updated, films_complete, films_not_found, films_errors = await enrich_media_type(
            db, MediaType.FILM, existing_external_ids, dry_run, user_id
        )

        if not dry_run:
            await db.commit()

        # ========================================
        # PART 3: Enrich Series with TMDB
        # ========================================
        print("\n" + "=" * 60)
        print("PART 3: Enriching Series with TMDB metadata")
        print("=" * 60)

        series_updated, series_complete, series_not_found, series_errors = await enrich_media_type(
            db, MediaType.SERIES, existing_external_ids, dry_run, user_id
        )

        if not dry_run:
            await db.commit()

        # ========================================
        # SUMMARY
        # ========================================
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:")
        print(f"\n  Cleanup:")
        print(f"    Podcasts deleted: {deleted_podcasts}")
        print(f"    Videos deleted: {deleted_videos}")

        print(f"\n  Films:")
        print(f"    Already complete: {films_complete}")
        print(f"    Updated: {films_updated}")
        print(f"    Not found on TMDB: {len(films_not_found)}")
        print(f"    Errors: {len(films_errors)}")

        print(f"\n  Series:")
        print(f"    Already complete: {series_complete}")
        print(f"    Updated: {series_updated}")
        print(f"    Not found on TMDB: {len(series_not_found)}")
        print(f"    Errors: {len(series_errors)}")

        # Combine not found lists
        all_not_found = films_not_found + series_not_found
        all_errors = films_errors + series_errors

        if all_not_found:
            print(f"\n--- NOT FOUND ON TMDB ({len(all_not_found)}) ---")
            if films_not_found:
                print("  [Films]")
                for title in films_not_found:
                    print(f"    - {title}")
            if series_not_found:
                print("  [Series]")
                for title in series_not_found:
                    print(f"    - {title}")

        if all_errors:
            print(f"\n--- ERRORS ({len(all_errors)}) ---")
            for error in all_errors:
                print(f"  - {error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup and enrich media")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without changes")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    args = parser.parse_args()

    asyncio.run(cleanup_and_enrich(
        dry_run=args.dry_run,
        user_id=args.user_id,
    ))
