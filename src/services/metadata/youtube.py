"""YouTube metadata service using yt-dlp for comprehensive data extraction."""

import asyncio
import re
from typing import Any

import httpx


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",  # Just the ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


class YouTubeService:
    """Service for fetching YouTube video metadata using yt-dlp."""

    def __init__(self) -> None:
        self.oembed_url = "https://www.youtube.com/oembed"

    async def get_video_info(self, url_or_id: str) -> dict[str, Any] | None:
        """
        Get video information from YouTube URL or video ID.

        Uses yt-dlp for comprehensive metadata (duration, description, upload date).
        Falls back to oEmbed if yt-dlp fails.

        Returns: title, channel_name, cover_url, video_id, duration_minutes, description, year
        """
        video_id = extract_video_id(url_or_id)
        if not video_id:
            return None

        video_url = f"https://www.youtube.com/watch?v={video_id}"

        # Try yt-dlp first (most comprehensive data)
        result = await self._extract_with_ytdlp(video_url, video_id)
        if result:
            return result

        # Fallback to oEmbed (limited data, no duration)
        return await self._extract_with_oembed(video_url, video_id)

    async def _extract_with_ytdlp(
        self, video_url: str, video_id: str
    ) -> dict[str, Any] | None:
        """Extract metadata using yt-dlp (runs in thread pool to avoid blocking)."""
        try:
            import yt_dlp

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": False,
            }

            def extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(video_url, download=False)

            # Run in thread pool to avoid blocking
            info = await asyncio.to_thread(extract)

            if not info:
                return None

            # Extract duration in minutes (yt-dlp returns seconds)
            duration_seconds = info.get("duration")
            duration_minutes = (
                round(duration_seconds / 60) if duration_seconds else None
            )

            # Extract year from upload date (format: YYYYMMDD)
            upload_date = info.get("upload_date", "")
            year = int(upload_date[:4]) if upload_date and len(upload_date) >= 4 else None

            # Get best thumbnail
            thumbnails = info.get("thumbnails", [])
            cover_url = self._select_best_thumbnail(thumbnails, video_id)

            # Extract tags and categories
            tags = info.get("tags", []) or []
            categories = info.get("categories", []) or []

            return {
                "video_id": video_id,
                "title": info.get("title", ""),
                "channel_name": info.get("channel", "") or info.get("uploader", ""),
                "channel_url": info.get("channel_url", "") or info.get("uploader_url", ""),
                "cover_url": cover_url,
                "external_url": video_url,
                "duration_minutes": duration_minutes,
                "duration_seconds": duration_seconds,
                "description": info.get("description", ""),
                "year": year,
                "upload_date": upload_date,
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "tags": tags[:10],  # Limit to 10 most relevant tags
                "categories": categories,
            }

        except Exception:
            # yt-dlp failed, will fall back to oEmbed
            return None

    async def _extract_with_oembed(
        self, video_url: str, video_id: str
    ) -> dict[str, Any] | None:
        """Fallback extraction using oEmbed API (limited data)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(
                    self.oembed_url,
                    params={"url": video_url, "format": "json"},
                )
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "video_id": video_id,
                        "title": data.get("title", ""),
                        "channel_name": data.get("author_name", ""),
                        "channel_url": data.get("author_url", ""),
                        "cover_url": self._get_best_thumbnail(video_id),
                        "external_url": video_url,
                        "duration_minutes": None,
                        "duration_seconds": None,
                        "description": None,
                        "year": None,
                        "upload_date": None,
                        "view_count": None,
                        "like_count": None,
                        "tags": [],
                        "categories": [],
                    }
            except Exception:
                pass

        return None

    def _select_best_thumbnail(
        self, thumbnails: list[dict], video_id: str
    ) -> str:
        """Select the best quality thumbnail from yt-dlp thumbnails list."""
        if not thumbnails:
            return self._get_best_thumbnail(video_id)

        # Sort by resolution (preference for larger)
        # yt-dlp provides thumbnails with 'width' and 'height' or 'preference'
        best = None
        best_score = 0

        for thumb in thumbnails:
            url = thumb.get("url", "")
            width = thumb.get("width", 0) or 0
            height = thumb.get("height", 0) or 0
            preference = thumb.get("preference", 0) or 0

            # Calculate score based on resolution
            score = (width * height) + (preference * 1000)

            if score > best_score and url:
                best_score = score
                best = url

        return best or self._get_best_thumbnail(video_id)

    def _get_best_thumbnail(self, video_id: str) -> str:
        """Get the best quality thumbnail URL directly from YouTube."""
        # maxresdefault is highest quality (1280x720)
        return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"


# Singleton instance
youtube_service = YouTubeService()
