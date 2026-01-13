"""YouTube Watch Later synchronization service."""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import async_session_maker
from src.db.crud.media import get_or_create_author
from src.models.media import Media, MediaStatus, MediaType, media_authors
from src.models.user import User
from src.models.youtube import YouTubeMetadata
from src.services.youtube.watch_later import youtube_watch_later_service

logger = logging.getLogger(__name__)


@dataclass
class YouTubeSyncResult:
    """Result of YouTube Watch Later sync."""

    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    message: str = ""


async def sync_youtube_for_user(
    db: AsyncSession,
    user: User,
    max_videos: int = 100,
) -> YouTubeSyncResult:
    """Sync YouTube Watch Later playlist for a user.

    Args:
        db: Database session
        user: User to sync for
        max_videos: Maximum number of videos to import

    Returns:
        YouTubeSyncResult with sync statistics
    """
    result = YouTubeSyncResult()

    if not user.youtube_refresh_token:
        result.message = "YouTube not connected"
        return result

    if not user.youtube_sync_enabled:
        result.message = "YouTube sync disabled"
        return result

    # Refresh access token
    access_token = await youtube_watch_later_service.refresh_access_token(
        user.youtube_refresh_token
    )

    if not access_token:
        result.message = "Failed to refresh YouTube token"
        result.errors = 1
        return result

    # Fetch videos from playlist
    videos = await youtube_watch_later_service.get_watch_later_videos(
        access_token,
        max_results=max_videos,
        playlist_id=user.youtube_playlist_id,
    )

    if not videos:
        if not user.youtube_playlist_id:
            result.message = "No playlist configured. The Watch Later playlist is not accessible via API - please configure a custom playlist ID."
        else:
            result.message = "No videos found in playlist or playlist not accessible"
        return result

    # Get video details (duration, etc.)
    video_ids = [v["video_id"] for v in videos]
    video_details = await youtube_watch_later_service.get_video_details(
        access_token,
        video_ids,
    )

    # Get existing YouTube videos for this user (by external_id which is the video_id)
    existing_query = select(Media.external_id).where(
        Media.user_id == user.id,
        Media.type == MediaType.YOUTUBE,
        Media.external_id.isnot(None),
    )
    existing_result = await db.execute(existing_query)
    existing_video_ids = {row[0] for row in existing_result.fetchall()}

    # Process each video
    for video in videos:
        video_id = video["video_id"]
        details = video_details.get(video_id, {})

        if video_id in existing_video_ids:
            # Already imported, skip
            result.skipped += 1
            continue

        try:
            # Create new media entry
            media = Media(
                user_id=user.id,
                title=video["title"],
                type=MediaType.YOUTUBE,
                external_id=video_id,
                external_url=f"https://www.youtube.com/watch?v={video_id}",
                cover_url=video["thumbnail_url"],
                description=video["description"][:2000] if video["description"] else None,
                duration_minutes=details.get("duration_minutes"),
                year=details.get("year"),
                status=MediaStatus.TO_CONSUME,
            )
            db.add(media)
            await db.flush()

            # Create YouTube metadata
            youtube_meta = YouTubeMetadata(
                media_id=media.id,
                video_id=video_id,
                channel_name=video["channel_name"],
                channel_id=video["channel_id"],
                playlist_item_id=video.get("playlist_item_id"),
            )
            db.add(youtube_meta)

            # Add channel as author (for display in UI)
            if video["channel_name"]:
                author = await get_or_create_author(
                    db,
                    name=video["channel_name"],
                    media_type=MediaType.YOUTUBE,
                    external_id=video["channel_id"],
                )
                # Insert directly into junction table to avoid lazy-load issues
                await db.execute(
                    media_authors.insert().values(media_id=media.id, author_id=author.id)
                )

            result.added += 1

        except Exception as e:
            logger.error(f"Error importing video {video_id}: {e}")
            await db.rollback()
            result.errors += 1

    if result.added > 0:
        await db.commit()
    result.message = f"Imported {result.added} new videos, skipped {result.skipped} existing"
    return result


async def sync_youtube_for_user_id(user_id: int) -> YouTubeSyncResult:
    """Sync YouTube Watch Later for a user by ID.

    Args:
        user_id: ID of the user to sync

    Returns:
        YouTubeSyncResult with sync statistics
    """
    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        if not user:
            return YouTubeSyncResult(message="User not found", errors=1)

        return await sync_youtube_for_user(db, user)


async def sync_all_youtube_users() -> dict:
    """Sync YouTube Watch Later for all users with sync enabled.

    Returns:
        Summary of sync results across all users
    """
    summary = {
        "users_synced": 0,
        "total_added": 0,
        "total_errors": 0,
    }

    async with async_session_maker() as db:
        # Get all users with YouTube sync enabled
        query = select(User).where(
            User.youtube_sync_enabled == True,  # noqa: E712
            User.youtube_refresh_token.isnot(None),
        )
        result = await db.execute(query)
        users = result.scalars().all()

        for user in users:
            try:
                sync_result = await sync_youtube_for_user(db, user)
                summary["users_synced"] += 1
                summary["total_added"] += sync_result.added
                summary["total_errors"] += sync_result.errors
                logger.info(f"YouTube sync for user {user.id}: {sync_result.message}")
            except Exception as e:
                summary["total_errors"] += 1
                logger.error(f"YouTube sync failed for user {user.id}: {e}")

    return summary


async def remove_video_from_playlist(
    db: AsyncSession,
    media: Media,
    user: User,
) -> bool:
    """Remove a YouTube video from the user's playlist.

    Called when a video is marked as consumed/finished.
    The media entry is kept in the database.

    Args:
        db: Database session
        media: The media entry (must be YouTube type)
        user: The user who owns the media

    Returns:
        True if successfully removed from playlist, False otherwise
    """
    if media.type != MediaType.YOUTUBE:
        return False

    if not user.youtube_refresh_token:
        logger.debug(f"User {user.id} has no YouTube token, skipping playlist removal")
        return False

    # Get YouTube metadata for this media
    query = select(YouTubeMetadata).where(YouTubeMetadata.media_id == media.id)
    result = await db.execute(query)
    youtube_meta = result.scalar_one_or_none()

    if not youtube_meta or not youtube_meta.playlist_item_id:
        logger.debug(f"No playlist_item_id for media {media.id}, skipping removal")
        return False

    # Refresh access token
    access_token = await youtube_watch_later_service.refresh_access_token(
        user.youtube_refresh_token
    )

    if not access_token:
        logger.error(f"Failed to refresh token for user {user.id}")
        return False

    # Remove from playlist
    success = await youtube_watch_later_service.remove_from_playlist(
        access_token,
        youtube_meta.playlist_item_id,
    )

    if success:
        # Clear the playlist_item_id since it's no longer in the playlist
        youtube_meta.playlist_item_id = None
        logger.info(f"Removed video {youtube_meta.video_id} from playlist for user {user.id}")

    return success
