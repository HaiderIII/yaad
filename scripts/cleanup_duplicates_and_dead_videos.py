#!/usr/bin/env python3
"""Cleanup duplicates and recover dead YouTube videos.

This script:
1. Finds and removes duplicate entries (same title + type + user)
2. Tries to recover dead YouTube videos by searching for new URLs

Usage:
    python scripts/cleanup_duplicates_and_dead_videos.py [--dry-run] [--user-id=ID]

Options:
    --dry-run   Show what would be done without making changes
    --user-id   Only process media for a specific user
"""

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import httpx

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.db.crud import get_or_create_author
from src.db.database import async_session_maker
from src.models.media import Media, MediaType
from src.services.metadata.youtube import youtube_service


def normalize_for_comparison(title: str) -> str:
    """Normalize title for duplicate detection."""
    title = title.lower().strip()
    # Remove common variations
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)  # Remove year
    title = re.sub(r'\s*-\s*season\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*-\s*saison\s*\d+\s*$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'[:\-–—\'\"\.,!?]', ' ', title)  # Remove punctuation
    title = re.sub(r'\s+', ' ', title)  # Normalize whitespace
    return title.strip()


def similarity(s1: str, s2: str) -> float:
    """Calculate similarity between two strings."""
    return SequenceMatcher(None, s1, s2).ratio()


def is_better_entry(entry1: Media, entry2: Media) -> bool:
    """Return True if entry1 is better than entry2 (more complete)."""
    score1 = 0
    score2 = 0

    # Cover
    if entry1.cover_url:
        score1 += 2
    if entry2.cover_url:
        score2 += 2

    # Description
    if entry1.description:
        score1 += 1
    if entry2.description:
        score2 += 1

    # Year
    if entry1.year:
        score1 += 1
    if entry2.year:
        score2 += 1

    # External ID
    if entry1.external_id:
        score1 += 2
    if entry2.external_id:
        score2 += 2

    # External URL
    if entry1.external_url:
        score1 += 1
    if entry2.external_url:
        score2 += 1

    # Authors
    if entry1.authors:
        score1 += 1
    if entry2.authors:
        score2 += 1

    # Genres
    if entry1.genres:
        score1 += 1
    if entry2.genres:
        score2 += 1

    # If scores are equal, prefer newer entry
    if score1 == score2:
        return entry1.created_at > entry2.created_at

    return score1 > score2


async def search_youtube_by_title(title: str, channel_hint: str | None = None) -> dict | None:
    """Search YouTube for a video by title.

    Returns video info if found, None otherwise.
    """
    try:
        # Use yt-dlp to search YouTube
        import subprocess
        import json

        # Build search query
        search_query = title
        if channel_hint:
            search_query = f"{title} {channel_hint}"

        # Limit search to avoid too many results
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            "--flat-playlist",
            "--playlist-end", "5",
            f"ytsearch5:{search_query}"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return None

        # Parse results
        results = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    data = json.loads(line)
                    results.append(data)
                except json.JSONDecodeError:
                    continue

        if not results:
            return None

        # Find best match by title similarity
        normalized_title = normalize_for_comparison(title)
        best_match = None
        best_score = 0

        for r in results:
            r_title = r.get("title", "")
            score = similarity(normalized_title, normalize_for_comparison(r_title))
            if score > best_score and score > 0.6:  # Minimum 60% similarity
                best_score = score
                best_match = r

        if best_match:
            video_id = best_match.get("id")
            if video_id:
                # Get full video info
                return await youtube_service.get_video_info(video_id)

        return None

    except Exception:
        return None


async def try_recover_dead_video(media: Media) -> tuple[bool, str, dict | None]:
    """Try to recover a dead YouTube video by searching for it.

    Returns: (success, message, new_info or None)
    """
    # Extract channel name from authors if available
    channel_hint = None
    if media.authors:
        channel_hint = media.authors[0].name

    # Search by title
    new_info = await search_youtube_by_title(media.title, channel_hint)

    if new_info:
        return True, f"Found: '{new_info.get('title', '')[:40]}'", new_info

    return False, "Could not find replacement video", None


async def cleanup_duplicates_and_dead_videos(
    dry_run: bool = False,
    user_id: int | None = None,
) -> None:
    """Cleanup duplicates and recover dead YouTube videos."""

    async with async_session_maker() as db:
        # Get all media
        query = select(Media)
        if user_id:
            query = query.where(Media.user_id == user_id)

        result = await db.execute(query)
        all_media = result.scalars().all()

        print(f"Found {len(all_media)} total media entries\n")

        # ========================================
        # PART 1: Find and remove duplicates
        # ========================================
        print("=" * 60)
        print("PART 1: Finding duplicates")
        print("=" * 60)

        # Group by (user_id, type, normalized_title)
        groups: dict[tuple, list[Media]] = defaultdict(list)
        for m in all_media:
            key = (m.user_id, m.type, normalize_for_comparison(m.title))
            groups[key].append(m)

        # Find groups with more than one entry
        duplicates_found = []
        for key, entries in groups.items():
            if len(entries) > 1:
                duplicates_found.append((key, entries))

        print(f"Found {len(duplicates_found)} groups with duplicates\n")

        total_deleted = 0
        deleted_by_type = defaultdict(int)

        for key, entries in duplicates_found:
            user_id_key, media_type, norm_title = key

            # Sort by quality (best first)
            entries.sort(key=lambda e: (
                bool(e.cover_url),
                bool(e.external_id),
                bool(e.description),
                bool(e.year),
            ), reverse=True)

            # Keep the best one, delete the rest
            best = entries[0]
            to_delete = entries[1:]

            print(f"[{media_type.value}] '{best.title[:40]}' - {len(entries)} entries")
            print(f"  KEEP: id={best.id} (cover={'✓' if best.cover_url else '✗'}, "
                  f"desc={'✓' if best.description else '✗'}, year={best.year or '✗'})")

            for dup in to_delete:
                print(f"  DELETE: id={dup.id} (cover={'✓' if dup.cover_url else '✗'}, "
                      f"desc={'✓' if dup.description else '✗'}, year={dup.year or '✗'})")

                if not dry_run:
                    await db.delete(dup)

                total_deleted += 1
                deleted_by_type[media_type.value] += 1

        if not dry_run and total_deleted > 0:
            await db.commit()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Deleted {total_deleted} duplicate entries:")
        for type_name, count in sorted(deleted_by_type.items()):
            print(f"  - {type_name}: {count}")

        # ========================================
        # PART 2: Also check for external_id duplicates
        # ========================================
        print("\n" + "=" * 60)
        print("PART 2: Finding external_id duplicates")
        print("=" * 60)

        # Refresh media list after deletions
        result = await db.execute(query)
        all_media = result.scalars().all()

        # Group by (user_id, type, external_id)
        ext_groups: dict[tuple, list[Media]] = defaultdict(list)
        for m in all_media:
            if m.external_id:
                key = (m.user_id, m.type, m.external_id)
                ext_groups[key].append(m)

        ext_duplicates = [(k, v) for k, v in ext_groups.items() if len(v) > 1]

        print(f"Found {len(ext_duplicates)} groups with same external_id\n")

        ext_deleted = 0

        for key, entries in ext_duplicates:
            user_id_key, media_type, ext_id = key

            # Sort by quality
            entries.sort(key=lambda e: (
                bool(e.cover_url),
                bool(e.description),
                bool(e.year),
            ), reverse=True)

            best = entries[0]
            to_delete = entries[1:]

            print(f"[{media_type.value}] external_id={ext_id}")
            print(f"  KEEP: '{best.title[:40]}' (id={best.id})")

            for dup in to_delete:
                print(f"  DELETE: '{dup.title[:40]}' (id={dup.id})")

                if not dry_run:
                    await db.delete(dup)

                ext_deleted += 1

        if not dry_run and ext_deleted > 0:
            await db.commit()

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Deleted {ext_deleted} external_id duplicates")

        # ========================================
        # PART 3: Try to recover dead YouTube videos
        # ========================================
        print("\n" + "=" * 60)
        print("PART 3: Recovering dead YouTube videos")
        print("=" * 60)

        # Refresh media list
        result = await db.execute(query)
        all_media = result.scalars().all()

        # Find podcasts/videos with YouTube URLs that might be dead
        # (missing cover or description usually means the video couldn't be fetched)
        dead_candidates = [
            m for m in all_media
            if m.type in [MediaType.PODCAST, MediaType.YOUTUBE]
            and m.external_url
            and ("youtube.com" in m.external_url or "youtu.be" in m.external_url)
            and (not m.cover_url or not m.description)
            and len(m.title) > 5  # Has a meaningful title to search with
        ]

        print(f"Found {len(dead_candidates)} potential dead YouTube videos to recover\n")

        recovered = 0
        not_recovered = []

        for i, media in enumerate(dead_candidates, 1):
            print(f"[{i}/{len(dead_candidates)}] {media.title[:45]:45}", end=" ")

            # First, try the current URL again (maybe it was temporary)
            video_id = None
            for pattern in [
                r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
                r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
            ]:
                match = re.search(pattern, media.external_url)
                if match:
                    video_id = match.group(1)
                    break

            if video_id:
                info = await youtube_service.get_video_info(video_id)
                if info and info.get("cover_url"):
                    # Video is actually accessible, just update info
                    if not dry_run:
                        media.cover_url = info.get("cover_url")
                        media.description = (info.get("description") or "")[:2000]
                        media.duration_minutes = info.get("duration_minutes")
                        media.year = info.get("year")

                        if info.get("channel_name"):
                            try:
                                author = await get_or_create_author(db, info["channel_name"], media.type)
                                media.authors = [author]
                            except Exception:
                                pass

                    print(f"- REFRESHED (video still alive)")
                    recovered += 1
                    await asyncio.sleep(0.5)
                    continue

            # Video is truly dead, try to find a replacement
            success, msg, new_info = await try_recover_dead_video(media)

            if success and new_info:
                new_video_id = new_info.get("video_id")
                if new_video_id:
                    new_url = f"https://www.youtube.com/watch?v={new_video_id}"

                    if not dry_run:
                        media.external_url = new_url
                        media.external_id = new_video_id
                        media.title = new_info.get("title") or media.title
                        media.cover_url = new_info.get("cover_url")
                        media.description = (new_info.get("description") or "")[:2000]
                        media.duration_minutes = new_info.get("duration_minutes")
                        media.year = new_info.get("year")

                        if new_info.get("channel_name"):
                            try:
                                author = await get_or_create_author(db, new_info["channel_name"], media.type)
                                media.authors = [author]
                            except Exception:
                                pass

                    print(f"- RECOVERED: {msg}")
                    recovered += 1
                else:
                    print(f"- FAILED: {msg}")
                    not_recovered.append(f"{media.title}: {msg}")
            else:
                print(f"- FAILED: {msg}")
                not_recovered.append(f"{media.title}: {msg}")

            await asyncio.sleep(1)  # Rate limiting for YouTube searches

        if not dry_run and recovered > 0:
            await db.commit()

        # ========================================
        # SUMMARY
        # ========================================
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        print(f"\n{'[DRY RUN] ' if dry_run else ''}Results:\n")
        print(f"  Duplicates removed: {total_deleted + ext_deleted}")
        print(f"  YouTube videos recovered/refreshed: {recovered}")
        print(f"  YouTube videos not recovered: {len(not_recovered)}")

        if not_recovered:
            print(f"\n--- NOT RECOVERED ({len(not_recovered)}) ---")
            for item in not_recovered[:20]:  # Limit output
                print(f"  - {item}")
            if len(not_recovered) > 20:
                print(f"  ... and {len(not_recovered) - 20} more")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup duplicates and recover dead YouTube videos")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without changes")
    parser.add_argument("--user-id", type=int, help="Only process media for a specific user")
    args = parser.parse_args()

    asyncio.run(cleanup_duplicates_and_dead_videos(
        dry_run=args.dry_run,
        user_id=args.user_id,
    ))
