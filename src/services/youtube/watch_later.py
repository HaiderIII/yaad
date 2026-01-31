"""YouTube Watch Later import service using YouTube Data API v3."""

import logging
from datetime import UTC, datetime
from typing import Any

from src.config import get_settings
from src.utils.http_client import get_general_client

settings = get_settings()
logger = logging.getLogger(__name__)

# YouTube API endpoints
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# YouTube OAuth scopes required for Watch Later access
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]


class YouTubeWatchLaterService:
    """Service for importing YouTube Watch Later playlist."""

    def __init__(self) -> None:
        self.client_id = settings.google_client_id
        self.client_secret = settings.google_client_secret

    async def refresh_access_token(self, refresh_token: str) -> str | None:
        """Refresh the access token using the refresh token.

        Args:
            refresh_token: The stored refresh token

        Returns:
            New access token or None if refresh failed
        """
        client = get_general_client()
        try:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("access_token")

            logger.error(f"Failed to refresh token: {response.status_code}")
            return None

        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            return None

    async def get_watch_later_videos(
        self,
        access_token: str,
        max_results: int = 50,
        playlist_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch videos from a YouTube playlist.

        Args:
            access_token: Valid OAuth2 access token
            max_results: Maximum number of videos to fetch (max 50 per page)
            playlist_id: Custom playlist ID. If None, tries "WL" (Watch Later).
                        Note: "WL" playlist is private and not accessible via API.
                        Use a custom playlist ID instead.

        Returns:
            List of video data dictionaries
        """
        # Use custom playlist or fall back to WL (which won't work)
        target_playlist = playlist_id or "WL"
        videos = []
        page_token = None

        client = get_general_client()
        while True:
            params = {
                "part": "snippet,contentDetails",
                "playlistId": target_playlist,
                "maxResults": min(max_results - len(videos), 50),
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                response = await client.get(
                    f"{YOUTUBE_API_BASE}/playlistItems",
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                if response.status_code == 403:
                    error_data = response.json()
                    error_reason = error_data.get("error", {}).get("errors", [{}])[0].get("reason")
                    if error_reason == "watchLaterNotAccessible":
                        logger.warning("Watch Later playlist not accessible via API")
                        return []
                    logger.error(f"YouTube API forbidden: {error_data}")
                    return videos

                if response.status_code != 200:
                    logger.error(f"YouTube API error: {response.status_code} - {response.text}")
                    break

                data = response.json()

                for item in data.get("items", []):
                    snippet = item.get("snippet", {})
                    content_details = item.get("contentDetails", {})
                    resource_id = snippet.get("resourceId", {})

                    if resource_id.get("kind") != "youtube#video":
                        continue

                    video_id = resource_id.get("videoId")
                    if not video_id:
                        continue

                    # Get video duration (requires separate API call)
                    video_data = {
                        "video_id": video_id,
                        "playlist_item_id": item.get("id"),  # For deletion from playlist
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "channel_name": snippet.get("videoOwnerChannelTitle", ""),
                        "channel_id": snippet.get("videoOwnerChannelId", ""),
                        "thumbnail_url": self._get_best_thumbnail(snippet.get("thumbnails", {})),
                        "added_at": snippet.get("publishedAt"),  # When added to playlist
                        "video_published_at": content_details.get("videoPublishedAt"),
                    }
                    videos.append(video_data)

                # Check for more pages
                page_token = data.get("nextPageToken")
                if not page_token or len(videos) >= max_results:
                    break

            except Exception as e:
                logger.error(f"Error fetching Watch Later: {e}")
                break

        return videos

    async def get_video_details(
        self,
        access_token: str,
        video_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Fetch detailed information for multiple videos.

        Args:
            access_token: Valid OAuth2 access token
            video_ids: List of video IDs to fetch details for

        Returns:
            Dict mapping video_id to video details
        """
        details = {}

        client = get_general_client()
        # Process in batches of 50 (API limit)
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]

            try:
                response = await client.get(
                    f"{YOUTUBE_API_BASE}/videos",
                    params={
                        "part": "snippet,contentDetails,statistics",
                        "id": ",".join(batch),
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )

                if response.status_code != 200:
                    logger.error(f"Error fetching video details: {response.status_code}")
                    continue

                data = response.json()

                for item in data.get("items", []):
                    video_id = item.get("id")
                    snippet = item.get("snippet", {})
                    content_details = item.get("contentDetails", {})
                    statistics = item.get("statistics", {})

                    # Parse duration (ISO 8601 format: PT1H2M3S)
                    duration_str = content_details.get("duration", "")
                    duration_minutes = self._parse_duration(duration_str)

                    # Parse publish date for year
                    published_at = snippet.get("publishedAt", "")
                    year = None
                    if published_at:
                        try:
                            year = int(published_at[:4])
                        except (ValueError, IndexError):
                            pass

                    details[video_id] = {
                        "duration_minutes": duration_minutes,
                        "year": year,
                        "view_count": int(statistics.get("viewCount", 0)),
                        "like_count": int(statistics.get("likeCount", 0)),
                        "tags": snippet.get("tags", [])[:10],
                        "category_id": snippet.get("categoryId"),
                    }

            except Exception as e:
                logger.error(f"Error fetching video details batch: {e}")

        return details

    async def remove_from_playlist(
        self,
        access_token: str,
        playlist_item_id: str,
    ) -> bool:
        """Remove a video from a playlist.

        Args:
            access_token: Valid OAuth2 access token
            playlist_item_id: The playlist item ID (not video ID)

        Returns:
            True if successfully removed, False otherwise
        """
        client = get_general_client()
        try:
            response = await client.delete(
                f"{YOUTUBE_API_BASE}/playlistItems",
                params={"id": playlist_item_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if response.status_code == 204:
                logger.info(f"Removed playlist item {playlist_item_id}")
                return True

            logger.error(f"Failed to remove playlist item: {response.status_code} - {response.text}")
            return False

        except Exception as e:
            logger.error(f"Error removing from playlist: {e}")
            return False

    def _get_best_thumbnail(self, thumbnails: dict) -> str:
        """Get the best quality thumbnail URL."""
        for quality in ["maxres", "standard", "high", "medium", "default"]:
            if quality in thumbnails:
                return thumbnails[quality].get("url", "")
        return ""

    def _parse_duration(self, duration_str: str) -> int | None:
        """Parse ISO 8601 duration to minutes.

        Examples: PT1H2M3S -> 62, PT30M -> 30, PT45S -> 1
        """
        if not duration_str or not duration_str.startswith("PT"):
            return None

        import re

        hours = 0
        minutes = 0
        seconds = 0

        hour_match = re.search(r"(\d+)H", duration_str)
        if hour_match:
            hours = int(hour_match.group(1))

        minute_match = re.search(r"(\d+)M", duration_str)
        if minute_match:
            minutes = int(minute_match.group(1))

        second_match = re.search(r"(\d+)S", duration_str)
        if second_match:
            seconds = int(second_match.group(1))

        total_minutes = hours * 60 + minutes + (1 if seconds >= 30 else 0)
        return total_minutes if total_minutes > 0 else 1  # At least 1 minute


# Singleton instance
youtube_watch_later_service = YouTubeWatchLaterService()
