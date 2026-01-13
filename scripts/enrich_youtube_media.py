#!/usr/bin/env python3
"""Enrich incomplete media entries that have YouTube URLs.

This script fetches metadata from YouTube for media entries that:
- Have a YouTube URL in external_url
- Are missing metadata (title, description, duration, cover, etc.)

It does NOT change the media type - a Podcast with a YouTube URL stays a Podcast.

Usage:
    python scripts/enrich_youtube_media.py [--all] [--user-id=ID] [--dry-run] [--force]

Options:
    --all       Process all media with YouTube URLs, even those with metadata
    --user-id   Only process media for a specific user
    --dry-run   Show what would be updated without making changes
    --force     Overwrite existing data with YouTube metadata (not just fill empty fields)
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import or_, select

from src.db.database import async_session_maker
from src.models.media import Media, MediaType
from src.services.metadata.youtube import youtube_service


def extract_youtube_id(url: str) -> str | None:
    """Extract YouTube video ID from URL."""
    if not url:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_incomplete(media: Media) -> bool:
    """Check if media is missing important metadata."""
    # Missing cover
    if not media.cover_url:
        return True
    # Missing duration (for videos/podcasts this is important)
    if not media.duration_minutes:
        return True
    # Missing description
    if not media.description:
        return True
    # Title looks like a placeholder or is very short
    if len(media.title) < 5:
        return True
    return False


async def enrich_youtube_media(
    process_all: bool = False,
    user_id: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Enrich media entries with YouTube metadata.

    Args:
        process_all: Process all media with YouTube URLs, not just incomplete
        user_id: Only process media for a specific user
        dry_run: Show what would be updated without making changes
        force: Overwrite existing data with YouTube metadata
    """
    async with async_session_maker() as db:
        # Build query - find media with YouTube URLs
        query = select(Media).where(
            Media.external_url.isnot(None),
            or_(
                Media.external_url.ilike("%youtube.com%"),
                Media.external_url.ilike("%youtu.be%"),
            ),
        )

        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        media_list = result.scalars().all()

        # Build a set of existing (user_id, type, external_id) to avoid duplicates
        existing_external_ids: dict[tuple[int, MediaType, str], int] = {}
        for m in media_list:
            if m.external_id:
                key = (m.user_id, m.type, m.external_id)
                existing_external_ids[key] = m.id

        print(f"Found {len(media_list)} media entries with YouTube URLs")

        # Filter to incomplete ones (unless --all or --force)
        if not process_all and not force:
            media_list = [m for m in media_list if is_incomplete(m)]
            print(f"Filtered to {len(media_list)} incomplete entries")

        if force:
            print("FORCE MODE: Will overwrite existing metadata with YouTube data")

        if not media_list:
            print("No media to process")
            return

        updated_count = 0
        error_count = 0
        skipped_count = 0
        duplicate_count = 0

        for i, media in enumerate(media_list, 1):
            video_id = extract_youtube_id(media.external_url)
            if not video_id:
                print(f"[{i}/{len(media_list)}] {media.title[:40]:40} - No valid YouTube ID in URL")
                skipped_count += 1
                continue

            print(f"[{i}/{len(media_list)}] {media.title[:40]:40} ({media.type.value})", end=" ")

            try:
                info = await youtube_service.get_video_info(video_id)

                if not info:
                    print("- YouTube API returned no data")
                    error_count += 1
                    continue

                # Track what we're updating
                updates = []

                # In force mode, overwrite everything. Otherwise only fill empty fields.

                # Cover
                if info.get("cover_url") and (force or not media.cover_url):
                    if not dry_run:
                        media.cover_url = info["cover_url"]
                    updates.append("cover")

                # Duration
                if info.get("duration_minutes") and (force or not media.duration_minutes):
                    if not dry_run:
                        media.duration_minutes = info["duration_minutes"]
                    updates.append(f"duration={info['duration_minutes']}min")

                # Description
                if info.get("description") and (force or not media.description):
                    desc = info["description"][:2000]  # Limit length
                    if not dry_run:
                        media.description = desc
                    updates.append("description")

                # Year
                if info.get("year") and (force or not media.year):
                    if not dry_run:
                        media.year = info["year"]
                    updates.append(f"year={info['year']}")

                # External ID - check for duplicates before updating
                if force or not media.external_id:
                    new_key = (media.user_id, media.type, video_id)
                    existing_media_id = existing_external_ids.get(new_key)
                    # Only update if no other media has this external_id, or if it's this same media
                    if existing_media_id is None or existing_media_id == media.id:
                        if not dry_run:
                            media.external_id = video_id
                            existing_external_ids[new_key] = media.id
                        updates.append("external_id")
                    else:
                        updates.append("external_id(skipped-duplicate)")
                        duplicate_count += 1

                # Title - in force mode always update, otherwise only if placeholder
                if info.get("title"):
                    if force or len(media.title) < 5:
                        if not dry_run:
                            media.title = info["title"]
                        updates.append("title")

                # Update author from channel name (for podcasts/videos)
                if info.get("channel_name") and force:
                    from src.db.crud import get_or_create_author

                    channel_name = info["channel_name"]
                    if not dry_run:
                        try:
                            # Get or create author with channel name
                            author = await get_or_create_author(db, channel_name, media.type)
                            # Clear existing authors and add new one
                            media.authors = [author]
                        except Exception:
                            pass  # Skip author update on error
                    updates.append(f"author={channel_name[:20]}")

                if updates:
                    print(f"- Updated: {', '.join(updates)}")
                    updated_count += 1
                else:
                    print("- Already complete")
                    skipped_count += 1

                # Rate limiting
                await asyncio.sleep(0.5)

            except Exception as e:
                print(f"- Error: {e}")
                error_count += 1

        if not dry_run:
            await db.commit()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Done!")
        print(f"  Updated: {updated_count}")
        print(f"  Skipped: {skipped_count}")
        print(f"  Errors:  {error_count}")
        if duplicate_count:
            print(f"  Duplicates skipped: {duplicate_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich media with YouTube metadata")
    parser.add_argument("--all", action="store_true", help="Process all YouTube media, not just incomplete")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without changes")
    parser.add_argument("--force", action="store_true", help="Overwrite existing data with YouTube metadata")
    args = parser.parse_args()

    asyncio.run(enrich_youtube_media(
        process_all=args.all,
        user_id=args.user_id,
        dry_run=args.dry_run,
        force=args.force,
    ))
