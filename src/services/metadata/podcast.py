"""Podcast metadata service using yt-dlp and RSS parsing."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def extract_podcast_info_from_url(url: str) -> dict[str, str | None]:
    """Extract platform and ID from podcast URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # Spotify
    if "spotify.com" in host:
        # https://open.spotify.com/episode/xxx or /show/xxx
        match = re.search(r"/(episode|show)/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return {"platform": "spotify", "type": match.group(1), "id": match.group(2)}

    # Deezer share links (link.deezer.com)
    if "link.deezer.com" in host:
        return {"platform": "deezer_share", "url": url}

    # Deezer
    if "deezer.com" in host:
        # https://www.deezer.com/episode/xxx or /show/xxx
        match = re.search(r"/(episode|show)/(\d+)", parsed.path)
        if match:
            return {"platform": "deezer", "type": match.group(1), "id": match.group(2)}

    # Apple Podcasts
    if "podcasts.apple.com" in host:
        # https://podcasts.apple.com/us/podcast/xxx/id123?i=456
        match = re.search(r"/podcast/[^/]+/id(\d+)", parsed.path)
        if match:
            episode_id = None
            if "i=" in url:
                ep_match = re.search(r"i=(\d+)", url)
                if ep_match:
                    episode_id = ep_match.group(1)
            return {"platform": "apple", "show_id": match.group(1), "episode_id": episode_id}

    # YouTube (podcasts on YouTube)
    if "youtube.com" in host or "youtu.be" in host:
        return {"platform": "youtube", "url": url}

    # RSS Feed (direct)
    if url.endswith(".xml") or url.endswith(".rss") or "/feed" in url or "/rss" in url:
        return {"platform": "rss", "url": url}

    # Generic URL - try as RSS
    return {"platform": "unknown", "url": url}


class PodcastService:
    """Service for fetching podcast metadata."""

    def __init__(self) -> None:
        self.timeout = 30.0  # Increased timeout for slower platforms

    async def get_episode_info(self, url: str) -> dict[str, Any] | None:
        """
        Get podcast episode information from URL.

        Supports: Spotify, Apple Podcasts, YouTube podcasts, RSS feeds.

        Returns: title, show_name, host, cover_url, duration_minutes, description,
                 year, episode_number, external_url
        """
        info = extract_podcast_info_from_url(url)
        platform = info.get("platform", "unknown")

        # Try platform-specific methods first
        if platform == "spotify":
            result = await self._extract_from_spotify(url)
            if result:
                return result

        if platform == "deezer":
            result = await self._extract_from_deezer(url)
            if result:
                return result

        if platform == "deezer_share":
            result = await self._extract_from_deezer_share(url)
            if result:
                return result

        # Try yt-dlp (works for YouTube, some other platforms)
        result = await self._extract_with_ytdlp(url)
        if result:
            return result

        # For RSS feeds, parse directly
        if platform == "rss" or platform == "unknown":
            result = await self._extract_from_rss(url)
            if result:
                return result

        return None

    async def search_podcasts(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search for podcasts using iTunes Search API.

        Returns list of shows with basic info.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    "https://itunes.apple.com/search",
                    params={
                        "term": query,
                        "media": "podcast",
                        "entity": "podcast",
                        "limit": limit,
                    },
                )
                if response.status_code == 200:
                    data = response.json()
                    results = []
                    for item in data.get("results", []):
                        results.append({
                            "show_id": str(item.get("collectionId", "")),
                            "title": item.get("collectionName", ""),
                            "host": item.get("artistName", ""),
                            "cover_url": item.get("artworkUrl600")
                            or item.get("artworkUrl100", ""),
                            "feed_url": item.get("feedUrl", ""),
                            "genre": item.get("primaryGenreName", ""),
                            "episode_count": item.get("trackCount", 0),
                            "external_url": item.get("collectionViewUrl", ""),
                        })
                    return results
            except Exception:
                pass
        return []

    async def get_show_episodes(
        self, feed_url: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        Get episodes from a podcast RSS feed.

        Returns list of episodes with metadata.
        """
        episodes = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(feed_url)
                if response.status_code == 200:
                    root = ET.fromstring(response.text)
                    channel = root.find("channel")
                    if channel is None:
                        return []

                    # Get show info
                    show_title = self._get_text(channel, "title") or ""
                    show_image = self._get_itunes_image(channel) or self._get_text(
                        channel, "image/url"
                    )
                    show_author = self._get_text(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author") or ""

                    # Parse episodes
                    for i, item in enumerate(channel.findall("item")):
                        if i >= limit:
                            break

                        # Get duration
                        duration_str = self._get_text(
                            item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"
                        )
                        duration_minutes = self._parse_duration(duration_str)

                        # Get publish date
                        pub_date = self._get_text(item, "pubDate") or ""
                        year = self._extract_year_from_date(pub_date)

                        # Get episode number
                        episode_num = self._get_text(
                            item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}episode"
                        )

                        # Get episode image or fall back to show image
                        episode_image = self._get_itunes_image(item) or show_image

                        # Get audio URL
                        enclosure = item.find("enclosure")
                        audio_url = enclosure.get("url") if enclosure is not None else None

                        episodes.append({
                            "title": self._get_text(item, "title") or "",
                            "show_name": show_title,
                            "host": show_author,
                            "cover_url": episode_image,
                            "duration_minutes": duration_minutes,
                            "description": self._get_text(item, "description")
                            or self._get_text(
                                item,
                                "{http://www.itunes.com/dtds/podcast-1.0.dtd}summary",
                            )
                            or "",
                            "year": year,
                            "episode_number": int(episode_num) if episode_num else None,
                            "external_url": self._get_text(item, "link") or audio_url,
                            "audio_url": audio_url,
                            "pub_date": pub_date,
                        })
            except Exception:
                pass

        return episodes

    async def _extract_from_spotify(self, url: str) -> dict[str, Any] | None:
        """Extract metadata from Spotify combining embed page and main page."""
        import json

        # Extract episode ID from URL
        info = extract_podcast_info_from_url(url)
        episode_id = info.get("id")
        if not episode_id:
            logger.warning(f"Could not extract episode ID from Spotify URL: {url}")
            return None

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }

            # Fetch both pages in parallel for speed
            embed_url = f"https://open.spotify.com/embed/episode/{episode_id}"
            main_url = f"https://open.spotify.com/episode/{episode_id}"

            try:
                logger.info(f"Fetching Spotify embed page: {embed_url}")
                # Fetch embed page (structured metadata)
                embed_response = await client.get(embed_url, headers=headers)
                logger.info(f"Spotify embed response status: {embed_response.status_code}")

                entity = {}
                if embed_response.status_code == 200:
                    match = re.search(
                        r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>',
                        embed_response.text,
                    )
                    if match:
                        next_data = json.loads(match.group(1))
                        entity = (
                            next_data.get("props", {})
                            .get("pageProps", {})
                            .get("state", {})
                            .get("data", {})
                            .get("entity", {})
                        )

                # Fetch main page (for description)
                description = ""
                main_response = await client.get(main_url, headers=headers)
                if main_response.status_code == 200:
                    # Extract description from meta tag or JSON
                    desc_match = re.search(
                        r'"description":"([^"]*)"', main_response.text
                    )
                    if desc_match:
                        # Decode unicode escapes
                        description = desc_match.group(1).encode().decode(
                            "unicode_escape"
                        )

                if not entity:
                    logger.warning(f"No entity data found in Spotify embed page, falling back to oEmbed")
                    return await self._extract_from_spotify_oembed(url)

                # Extract title (remove date suffix if present)
                full_title = entity.get("name", "") or entity.get("title", "")
                title = full_title
                date_match = re.search(r" - \d{1,2}/\d{1,2}/\d{4}$", full_title)
                if date_match:
                    title = full_title[: date_match.start()]

                # Extract show name from subtitle
                show_name = entity.get("subtitle", "")

                # Duration in milliseconds -> minutes
                duration_ms = entity.get("duration")
                duration_minutes = round(duration_ms / 60000) if duration_ms else None
                duration_seconds = round(duration_ms / 1000) if duration_ms else None

                # Release date
                release_date = entity.get("releaseDate", {}).get("isoString", "")
                year = None
                if release_date:
                    year_match = re.search(r"(\d{4})", release_date)
                    if year_match:
                        year = int(year_match.group(1))

                # Cover image (get largest)
                cover_url = ""
                images = entity.get("relatedEntityCoverArt", [])
                if images:
                    images_sorted = sorted(
                        images, key=lambda x: x.get("maxHeight", 0), reverse=True
                    )
                    cover_url = images_sorted[0].get("url", "")

                # If no cover from related entity, try video thumbnail
                if not cover_url:
                    video_thumbs = entity.get("videoThumbnailImage", [])
                    if video_thumbs:
                        video_thumbs_sorted = sorted(
                            video_thumbs, key=lambda x: x.get("maxHeight", 0), reverse=True
                        )
                        cover_url = video_thumbs_sorted[0].get("url", "")

                # Use placeholder if no description found
                if not description:
                    description = "Description absente"

                return {
                    "title": title,
                    "show_name": show_name,
                    "host": show_name,
                    "cover_url": cover_url,
                    "external_url": url,
                    "duration_minutes": duration_minutes,
                    "duration_seconds": duration_seconds,
                    "description": description,
                    "year": year,
                    "episode_number": None,
                    "categories": [],
                    "tags": [],
                    "provider": "spotify",
                }
            except Exception as e:
                logger.error(f"Spotify extraction failed: {e}", exc_info=True)
                return await self._extract_from_spotify_oembed(url)

    async def _extract_from_deezer_share(self, url: str) -> dict[str, Any] | None:
        """Resolve Deezer share link and extract metadata."""
        from urllib.parse import parse_qs, unquote

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            try:
                # Follow the redirect to get the real URL
                response = await client.head(url)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    # The location contains the real URL in the 'dest' parameter
                    if "dest=" in location:
                        parsed = urlparse(location)
                        params = parse_qs(parsed.query)
                        if "dest" in params:
                            real_url = unquote(params["dest"][0])
                            logger.info(f"Resolved Deezer share link to: {real_url}")
                            return await self._extract_from_deezer(real_url)
                    # Or it might be a direct redirect
                    elif "deezer.com" in location:
                        return await self._extract_from_deezer(location)
            except Exception as e:
                logger.error(f"Failed to resolve Deezer share link: {e}")

        return None

    async def _extract_from_deezer(self, url: str) -> dict[str, Any] | None:
        """Extract metadata from Deezer podcast episode."""

        info = extract_podcast_info_from_url(url)
        episode_id = info.get("id")
        content_type = info.get("type", "episode")

        if not episode_id:
            return None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                # Deezer has a public API
                if content_type == "episode":
                    api_url = f"https://api.deezer.com/episode/{episode_id}"
                else:
                    # It's a show, get its info
                    api_url = f"https://api.deezer.com/podcast/{episode_id}"

                response = await client.get(api_url)
                if response.status_code != 200:
                    return None

                data = response.json()

                # Check for API error
                if "error" in data:
                    return None

                if content_type == "episode":
                    # Episode data
                    title = data.get("title", "")
                    description = data.get("description", "") or "Description absente"

                    # Duration in seconds
                    duration_seconds = data.get("duration")
                    duration_minutes = round(duration_seconds / 60) if duration_seconds else None

                    # Cover image
                    cover_url = (
                        data.get("picture_xl")
                        or data.get("picture_big")
                        or data.get("picture_medium")
                        or data.get("picture", "")
                    )

                    # Release date
                    release_date = data.get("release_date", "")
                    year = None
                    if release_date:
                        year_match = re.search(r"(\d{4})", release_date)
                        if year_match:
                            year = int(year_match.group(1))

                    # Get podcast/show info
                    podcast_data = data.get("podcast", {})
                    show_name = podcast_data.get("title", "")

                    # If no cover from episode, use podcast cover
                    if not cover_url:
                        cover_url = (
                            podcast_data.get("picture_xl")
                            or podcast_data.get("picture_big")
                            or podcast_data.get("picture_medium")
                            or podcast_data.get("picture", "")
                        )

                    return {
                        "title": title,
                        "show_name": show_name,
                        "host": show_name,
                        "cover_url": cover_url,
                        "external_url": data.get("link") or url,
                        "duration_minutes": duration_minutes,
                        "duration_seconds": duration_seconds,
                        "description": description,
                        "year": year,
                        "episode_number": None,
                        "categories": [],
                        "tags": [],
                        "provider": "deezer",
                    }
                else:
                    # Show/podcast data - return first episode or show info
                    title = data.get("title", "")
                    description = data.get("description", "") or "Description absente"

                    cover_url = (
                        data.get("picture_xl")
                        or data.get("picture_big")
                        or data.get("picture_medium")
                        or data.get("picture", "")
                    )

                    return {
                        "title": title,
                        "show_name": title,
                        "host": title,
                        "cover_url": cover_url,
                        "external_url": data.get("link") or url,
                        "duration_minutes": None,
                        "duration_seconds": None,
                        "description": description,
                        "year": None,
                        "episode_number": None,
                        "categories": [],
                        "tags": [],
                        "provider": "deezer",
                    }
            except Exception:
                return None

    async def _extract_from_spotify_oembed(self, url: str) -> dict[str, Any] | None:
        """Fallback: Extract basic metadata from Spotify oEmbed API."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    "https://open.spotify.com/oembed",
                    params={"url": url},
                )
                if response.status_code != 200:
                    return None

                data = response.json()
                full_title = data.get("title", "")
                title = full_title
                year = None

                # Check if title ends with a date pattern
                date_match = re.search(r" - (\d{1,2}/\d{1,2}/(\d{4}))$", full_title)
                if date_match:
                    title = full_title[: date_match.start()]
                    year = int(date_match.group(2))

                return {
                    "title": title,
                    "show_name": "",
                    "host": "",
                    "cover_url": data.get("thumbnail_url", ""),
                    "external_url": url,
                    "duration_minutes": None,
                    "duration_seconds": None,
                    "description": "Description absente",
                    "year": year,
                    "episode_number": None,
                    "categories": [],
                    "tags": [],
                    "provider": "spotify",
                }
            except Exception:
                return None

    async def _extract_with_ytdlp(self, url: str) -> dict[str, Any] | None:
        """Extract metadata using yt-dlp (runs in thread pool)."""
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
                    return ydl.extract_info(url, download=False)

            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, extract)

            if not info:
                return None

            # Extract duration in minutes
            duration_seconds = info.get("duration")
            duration_minutes = (
                round(duration_seconds / 60) if duration_seconds else None
            )

            # Extract year from upload/release date
            upload_date = info.get("upload_date") or info.get("release_date", "")
            year = int(upload_date[:4]) if upload_date and len(upload_date) >= 4 else None

            # Get thumbnail
            thumbnails = info.get("thumbnails", [])
            cover_url = self._select_best_thumbnail(thumbnails)
            if not cover_url:
                cover_url = info.get("thumbnail", "")

            # Determine show name and episode title
            # yt-dlp may return these differently based on platform
            title = info.get("title", "")
            show_name = (
                info.get("series")
                or info.get("album")
                or info.get("playlist_title")
                or info.get("channel")
                or ""
            )
            host = (
                info.get("artist")
                or info.get("creator")
                or info.get("uploader")
                or info.get("channel")
                or ""
            )

            # Episode number
            episode_number = info.get("episode_number") or info.get("playlist_index")

            # Categories/genres from yt-dlp
            categories = info.get("categories", []) or []
            tags = info.get("tags", []) or []

            return {
                "title": title,
                "show_name": show_name,
                "host": host,
                "cover_url": cover_url,
                "external_url": info.get("webpage_url") or url,
                "duration_minutes": duration_minutes,
                "duration_seconds": duration_seconds,
                "description": info.get("description", ""),
                "year": year,
                "episode_number": episode_number,
                "categories": categories,
                "tags": tags[:10],
            }

        except Exception:
            return None

    async def _extract_from_rss(self, url: str) -> dict[str, Any] | None:
        """Try to extract podcast info from RSS feed URL."""
        # First check if it's an RSS feed
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    return None

                content_type = response.headers.get("content-type", "")
                content = response.text

                # Check if it's RSS/XML
                if "xml" not in content_type and not content.strip().startswith("<?xml"):
                    return None

                root = ET.fromstring(content)
                channel = root.find("channel")
                if channel is None:
                    return None

                # Get latest episode (first item)
                item = channel.find("item")
                if item is None:
                    return None

                # Show info
                show_title = self._get_text(channel, "title") or ""
                show_image = self._get_itunes_image(channel) or self._get_text(
                    channel, "image/url"
                )
                show_author = self._get_text(
                    channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author"
                ) or ""

                # Episode info
                duration_str = self._get_text(
                    item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration"
                )
                duration_minutes = self._parse_duration(duration_str)

                pub_date = self._get_text(item, "pubDate") or ""
                year = self._extract_year_from_date(pub_date)

                episode_num = self._get_text(
                    item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}episode"
                )

                episode_image = self._get_itunes_image(item) or show_image

                enclosure = item.find("enclosure")
                audio_url = enclosure.get("url") if enclosure is not None else None

                # Categories
                categories = []
                for cat in channel.findall(
                    "{http://www.itunes.com/dtds/podcast-1.0.dtd}category"
                ):
                    cat_text = cat.get("text")
                    if cat_text:
                        categories.append(cat_text)

                return {
                    "title": self._get_text(item, "title") or "",
                    "show_name": show_title,
                    "host": show_author,
                    "cover_url": episode_image,
                    "external_url": self._get_text(item, "link") or audio_url or url,
                    "duration_minutes": duration_minutes,
                    "duration_seconds": duration_minutes * 60 if duration_minutes else None,
                    "description": self._get_text(item, "description")
                    or self._get_text(
                        item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}summary"
                    )
                    or "",
                    "year": year,
                    "episode_number": int(episode_num) if episode_num else None,
                    "categories": categories,
                    "tags": [],
                    "feed_url": url,
                }
            except Exception:
                return None

    def _get_text(self, element: ET.Element, path: str) -> str | None:
        """Get text content from XML element."""
        el = element.find(path)
        if el is not None and el.text:
            return el.text.strip()
        return None

    def _get_itunes_image(self, element: ET.Element) -> str | None:
        """Get iTunes image URL from element."""
        img = element.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
        if img is not None:
            return img.get("href")
        return None

    def _parse_duration(self, duration_str: str | None) -> int | None:
        """Parse duration string to minutes."""
        if not duration_str:
            return None

        try:
            # Format: HH:MM:SS or MM:SS or seconds
            parts = duration_str.split(":")
            if len(parts) == 3:
                hours, mins, secs = map(int, parts)
                return hours * 60 + mins + (1 if secs >= 30 else 0)
            elif len(parts) == 2:
                mins, secs = map(int, parts)
                return mins + (1 if secs >= 30 else 0)
            else:
                # Just seconds
                return round(int(duration_str) / 60)
        except (ValueError, TypeError):
            return None

    def _extract_year_from_date(self, date_str: str) -> int | None:
        """Extract year from various date formats."""
        if not date_str:
            return None

        # Try to find 4-digit year
        match = re.search(r"\b(19|20)\d{2}\b", date_str)
        if match:
            return int(match.group())
        return None

    def _select_best_thumbnail(self, thumbnails: list[dict]) -> str | None:
        """Select the best quality thumbnail from yt-dlp thumbnails list."""
        if not thumbnails:
            return None

        best = None
        best_score = 0

        for thumb in thumbnails:
            url = thumb.get("url", "")
            width = thumb.get("width", 0) or 0
            height = thumb.get("height", 0) or 0
            preference = thumb.get("preference", 0) or 0

            score = (width * height) + (preference * 1000)

            if score > best_score and url:
                best_score = score
                best = url

        return best


# Singleton instance
podcast_service = PodcastService()
