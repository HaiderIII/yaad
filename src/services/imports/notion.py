"""Notion CSV import service."""

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud import create_media
from src.models.media import Media, MediaStatus, MediaType
from src.models.schemas import MediaCreate
from src.services.metadata.books import book_service
from src.services.metadata.podcast import podcast_service
from src.services.metadata.tmdb import tmdb_service
from src.services.metadata.youtube import youtube_service

logger = logging.getLogger(__name__)


# Mapping from Notion types to our MediaType
NOTION_TYPE_MAPPING = {
    # Films
    "film": MediaType.FILM,
    "films": MediaType.FILM,
    "movie": MediaType.FILM,
    "movies": MediaType.FILM,
    "cinema": MediaType.FILM,
    "cinéma": MediaType.FILM,
    # Books
    "livre": MediaType.BOOK,
    "livres": MediaType.BOOK,
    "book": MediaType.BOOK,
    "books": MediaType.BOOK,
    "roman": MediaType.BOOK,
    "bd": MediaType.BOOK,
    "manga": MediaType.BOOK,
    "comic": MediaType.BOOK,
    "comics": MediaType.BOOK,
    # TV Series
    "tv series": MediaType.SERIES,
    "tv show": MediaType.SERIES,
    "tv shows": MediaType.SERIES,
    "série": MediaType.SERIES,
    "séries": MediaType.SERIES,
    "series": MediaType.SERIES,
    "serie": MediaType.SERIES,
    "show": MediaType.SERIES,
    "shows": MediaType.SERIES,
    "anime": MediaType.SERIES,
    "animé": MediaType.SERIES,
    "animation": MediaType.SERIES,
    "serie / animé": MediaType.SERIES,
    "série / animé": MediaType.SERIES,
    "serie / anime": MediaType.SERIES,
    "série / anime": MediaType.SERIES,
    # Podcasts
    "discussion": MediaType.PODCAST,  # Discussion = Podcast
    "podcast": MediaType.PODCAST,
    "podcasts": MediaType.PODCAST,
    "audio": MediaType.PODCAST,
    # YouTube/Video
    "reportage": MediaType.YOUTUBE,  # Reportage = YouTube video
    "reportages": MediaType.YOUTUBE,
    "video": MediaType.YOUTUBE,
    "vidéo": MediaType.YOUTUBE,
    "videos": MediaType.YOUTUBE,
    "vidéos": MediaType.YOUTUBE,
    "youtube": MediaType.YOUTUBE,
    "documentaire": MediaType.YOUTUBE,
    "documentary": MediaType.YOUTUBE,
    # Skip these types
    "article": None,
    "articles": None,
}

# Mapping from Notion status to our MediaStatus
NOTION_STATUS_MAPPING = {
    "finished": MediaStatus.FINISHED,
    "terminé": MediaStatus.FINISHED,
    "done": MediaStatus.FINISHED,
    "vu": MediaStatus.FINISHED,
    "lu": MediaStatus.FINISHED,
    "ready to start": MediaStatus.TO_CONSUME,
    "to watch": MediaStatus.TO_CONSUME,
    "to read": MediaStatus.TO_CONSUME,
    "à voir": MediaStatus.TO_CONSUME,
    "à lire": MediaStatus.TO_CONSUME,
    "in progress": MediaStatus.IN_PROGRESS,
    "en cours": MediaStatus.IN_PROGRESS,
    "watching": MediaStatus.IN_PROGRESS,
    "reading": MediaStatus.IN_PROGRESS,
    "abandoned": MediaStatus.ABANDONED,
    "abandonné": MediaStatus.ABANDONED,
}


@dataclass
class NotionEntry:
    """Parsed Notion entry."""

    name: str
    type: str | None
    author: str | None
    status: str | None
    link: str | None
    score: float | None
    date: datetime | None


@dataclass
class ImportResult:
    """Result of an import operation."""

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] | None = None


class NotionImporter:
    """Import media from Notion CSV exports."""

    def parse_csv(self, content: str) -> list[NotionEntry]:
        """Parse Notion CSV content.

        Expected columns (case-insensitive):
        - Name: Title of the media
        - Type: Film, Livre, TV Series, Discussion, Reportage, Article
        - Author: Author/Director name
        - Status: Finished, Ready to Start, In Progress, etc.
        - Link: URL (YouTube, podcast, article URL)
        - Score: Rating (numeric)
        - Date: Watched/read date

        Args:
            content: CSV file content as string

        Returns:
            List of parsed entries
        """
        entries = []

        # Detect delimiter (comma or semicolon - French CSVs use semicolon)
        first_line = content.split('\n')[0] if content else ''
        delimiter = ';' if first_line.count(';') > first_line.count(',') else ','

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        # Normalize header names (case-insensitive)
        if reader.fieldnames:
            header_map = {h.lower().strip(): h for h in reader.fieldnames}
            logger.info(f"CSV columns detected: {list(reader.fieldnames)}")
        else:
            return entries

        for row in reader:
            try:
                # Get values using normalized headers
                # Use exact match first, then partial match
                def get_value(*keys: str) -> str | None:
                    # First try exact match
                    for key in keys:
                        key_lower = key.lower()
                        if key_lower in header_map:
                            val = row.get(header_map[key_lower], "").strip()
                            if val:
                                return val
                    # Then try partial match (key contained in column name)
                    for key in keys:
                        key_lower = key.lower()
                        for k, original in header_map.items():
                            if key_lower in k and k != key_lower:
                                val = row.get(original, "").strip()
                                if val:
                                    return val
                    return None

                name = get_value("name", "titre", "title")
                if not name:
                    continue

                # Parse type - look for exact "type" column or "catégorie" or "category"
                type_str = get_value("type", "catégorie", "category", "categorie", "media type", "media_type")

                # Parse score
                score = None
                score_str = get_value("score", "note", "rating")
                if score_str:
                    try:
                        # Handle various formats: "8", "8/10", "4.5"
                        score_str = score_str.replace(",", ".")
                        if "/" in score_str:
                            parts = score_str.split("/")
                            score = float(parts[0]) / float(parts[1]) * 10
                        else:
                            score = float(score_str)
                            # Normalize to 0-10 scale if appears to be 0-5
                            if score <= 5:
                                score = score * 2
                    except (ValueError, ZeroDivisionError):
                        pass

                # Parse date
                date = None
                date_str = get_value("date", "watched", "read", "finished")
                if date_str:
                    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y"]:
                        try:
                            date = datetime.strptime(date_str, fmt)
                            break
                        except ValueError:
                            continue

                entry = NotionEntry(
                    name=name,
                    type=type_str,
                    author=get_value("author", "auteur", "director", "réalisateur", "creator"),
                    status=get_value("status", "statut", "state", "état"),
                    link=get_value("link", "url", "lien"),
                    score=score,
                    date=date,
                )
                entries.append(entry)

            except (ValueError, KeyError) as e:
                logger.warning(f"Failed to parse row: {row}, error: {e}")
                continue

        return entries

    def _get_media_type(self, entry: NotionEntry) -> MediaType | None:
        """Determine MediaType from Notion entry."""
        if entry.type:
            type_lower = entry.type.lower().strip()
            return NOTION_TYPE_MAPPING.get(type_lower)

        # Try to infer from link
        if entry.link:
            link_lower = entry.link.lower()
            if "youtube.com" in link_lower or "youtu.be" in link_lower:
                return MediaType.YOUTUBE
            if "spotify.com" in link_lower or "podcast" in link_lower:
                return MediaType.PODCAST

        return None

    def _get_status(self, entry: NotionEntry) -> MediaStatus:
        """Determine MediaStatus from Notion entry."""
        if entry.status:
            status_lower = entry.status.lower().strip()
            return NOTION_STATUS_MAPPING.get(status_lower, MediaStatus.TO_CONSUME)

        # If has score or date, assume finished
        if entry.score or entry.date:
            return MediaStatus.FINISHED

        return MediaStatus.TO_CONSUME

    async def import_single_entry(
        self,
        db: AsyncSession,
        user_id: int,
        entry: NotionEntry,
        skip_existing: bool = True,
        fetch_metadata: bool = True,
    ) -> tuple[str, str | None]:
        """Import a single entry into the database.

        Args:
            db: Database session
            user_id: User ID to import for
            entry: Single Notion entry
            skip_existing: Skip if already exists
            fetch_metadata: Fetch metadata from external APIs

        Returns:
            Tuple of (status, error_message) where status is 'imported', 'skipped', or 'failed'
        """
        from sqlalchemy import or_
        from sqlalchemy.exc import IntegrityError

        try:
            media_type = self._get_media_type(entry)
            if media_type is None:
                return ("skipped", f"Skipped (unsupported type '{entry.type}'): {entry.name}")

            status = self._get_status(entry)

            # Build media data based on type
            media_data, genres, authors = await self._build_media_data(
                entry, media_type, status, fetch_metadata
            )

            if media_data is None:
                return ("failed", f"Could not find metadata: {entry.name}")

            # Check if already exists
            if skip_existing:
                conditions = [Media.title.ilike(entry.name)]
                if media_data.external_id:
                    conditions.append(Media.external_id == media_data.external_id)

                existing = await db.execute(
                    select(Media).where(
                        Media.user_id == user_id,
                        Media.type == media_type,
                        or_(*conditions),
                    )
                )
                existing_media = existing.scalar_one_or_none()
                if existing_media:
                    logger.info(f"Skipping '{entry.name}' - already exists as '{existing_media.title}' (id={existing_media.id})")
                    return ("skipped", None)

            # Create media entry
            await create_media(
                db=db,
                user_id=user_id,
                data=media_data,
                genres=genres,
                authors=authors,
            )
            await db.flush()

            return ("imported", None)

        except IntegrityError as e:
            await db.rollback()
            logger.warning(f"Duplicate entry skipped: {entry.name} - {e}")
            return ("skipped", None)
        except Exception as e:
            await db.rollback()
            logger.exception(f"Failed to import: {entry.name}")
            return ("failed", f"{entry.name}: {str(e)}")

    async def _build_media_data(
        self,
        entry: NotionEntry,
        media_type: MediaType,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build MediaCreate from Notion entry, fetching metadata from appropriate API.

        Returns:
            Tuple of (MediaCreate, genres, authors)
        """
        if media_type == MediaType.FILM:
            return await self._build_film_data(entry, status, fetch_metadata)
        elif media_type == MediaType.SERIES:
            return await self._build_series_data(entry, status, fetch_metadata)
        elif media_type == MediaType.BOOK:
            return await self._build_book_data(entry, status, fetch_metadata)
        elif media_type == MediaType.YOUTUBE:
            return await self._build_youtube_data(entry, status, fetch_metadata)
        elif media_type == MediaType.PODCAST:
            return await self._build_podcast_data(entry, status, fetch_metadata)
        else:
            return None, None, None

    async def _build_film_data(
        self,
        entry: NotionEntry,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build film media data."""
        if fetch_metadata:
            # Search TMDB for movies
            results = await tmdb_service.search_movies(entry.name)
            if results:
                tmdb_id = results[0]["id"]
                details = await tmdb_service.get_movie_details(tmdb_id)
                if details:
                    genres = details.get("genres", [])
                    directors = [d["name"] for d in details.get("directors", [])]

                    return MediaCreate(
                        title=details.get("original_title") or details.get("title") or entry.name,
                        local_title=details.get("local_title"),
                        type=MediaType.FILM,
                        external_id=str(tmdb_id),
                        year=int(details.get("year")) if details.get("year") else None,
                        duration_minutes=details.get("duration_minutes"),
                        description=details.get("description"),
                        cover_url=details.get("cover_url"),
                        status=status,
                        rating=entry.score,
                        consumed_at=entry.date,
                        tmdb_rating=details.get("tmdb_rating"),
                        tmdb_vote_count=details.get("tmdb_vote_count"),
                        popularity=details.get("popularity"),
                        original_language=details.get("original_language"),
                        tagline=details.get("tagline"),
                        cast=details.get("cast"),
                        keywords=details.get("keywords"),
                        certification=details.get("certification"),
                    ), genres, directors

        # Fallback: create basic entry
        return MediaCreate(
            title=entry.name,
            type=MediaType.FILM,
            status=status,
            rating=entry.score,
            consumed_at=entry.date,
        ), None, [entry.author] if entry.author else None

    async def _build_series_data(
        self,
        entry: NotionEntry,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build TV series media data."""
        if fetch_metadata:
            results = await tmdb_service.search_tv(entry.name)
            if results:
                tmdb_id = results[0]["id"]
                details = await tmdb_service.get_tv_details(tmdb_id)
                if details:
                    genres = details.get("genres", [])
                    creators = [c["name"] for c in details.get("creators", [])]

                    return MediaCreate(
                        title=details.get("original_title") or details.get("title") or entry.name,
                        local_title=details.get("local_title"),
                        type=MediaType.SERIES,
                        external_id=str(tmdb_id),
                        year=int(details.get("year")) if details.get("year") else None,
                        description=details.get("description"),
                        cover_url=details.get("cover_url"),
                        status=status,
                        rating=entry.score,
                        consumed_at=entry.date,
                        tmdb_rating=details.get("tmdb_rating"),
                        tmdb_vote_count=details.get("tmdb_vote_count"),
                        popularity=details.get("popularity"),
                        original_language=details.get("original_language"),
                        number_of_seasons=details.get("number_of_seasons"),
                        number_of_episodes=details.get("number_of_episodes"),
                        series_status=details.get("series_status"),
                        networks=details.get("networks"),
                    ), genres, creators

        return MediaCreate(
            title=entry.name,
            type=MediaType.SERIES,
            status=status,
            rating=entry.score,
            consumed_at=entry.date,
        ), None, [entry.author] if entry.author else None

    async def _build_book_data(
        self,
        entry: NotionEntry,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build book media data."""
        if fetch_metadata:
            # Search using book_service (combines Google Books and Open Library)
            query = entry.name
            if entry.author:
                query = f"{entry.name} {entry.author}"

            results = await book_service.search_books(query, limit=5)
            if results:
                book = results[0]
                # Use the search result data directly
                # book_service returns complete data from search
                isbn = book.get("isbn") or book.get("external_id")

                return MediaCreate(
                    title=book.get("title") or entry.name,
                    type=MediaType.BOOK,
                    external_id=isbn,
                    year=book.get("year"),
                    page_count=book.get("page_count"),
                    description=book.get("description"),
                    cover_url=book.get("cover_url"),
                    status=status,
                    rating=entry.score,
                    consumed_at=entry.date,
                ), None, book.get("authors", [])

        return MediaCreate(
            title=entry.name,
            type=MediaType.BOOK,
            status=status,
            rating=entry.score,
            consumed_at=entry.date,
        ), None, [entry.author] if entry.author else None

    async def _build_youtube_data(
        self,
        entry: NotionEntry,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build YouTube video media data."""
        video_id = None

        # Extract video ID from link if present
        if entry.link:
            video_id = self._extract_youtube_id(entry.link)

        if fetch_metadata and video_id:
            details = await youtube_service.get_video_details(video_id)
            if details:
                return MediaCreate(
                    title=details.get("title") or entry.name,
                    type=MediaType.YOUTUBE,
                    external_id=video_id,
                    external_url=f"https://www.youtube.com/watch?v={video_id}",
                    duration_minutes=details.get("duration_minutes"),
                    description=details.get("description"),
                    cover_url=details.get("cover_url"),
                    status=status,
                    rating=entry.score,
                    consumed_at=entry.date,
                ), None, [details.get("channel_name")] if details.get("channel_name") else None

        # Fallback
        return MediaCreate(
            title=entry.name,
            type=MediaType.YOUTUBE,
            external_id=video_id,
            external_url=entry.link,
            status=status,
            rating=entry.score,
            consumed_at=entry.date,
        ), None, [entry.author] if entry.author else None

    async def _build_podcast_data(
        self,
        entry: NotionEntry,
        status: MediaStatus,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build podcast media data."""
        if fetch_metadata:
            # Search for podcast
            results = await podcast_service.search_podcasts(entry.name)
            if results:
                podcast = results[0]
                return MediaCreate(
                    title=podcast.get("title") or entry.name,
                    type=MediaType.PODCAST,
                    external_id=podcast.get("id"),
                    external_url=podcast.get("url") or entry.link,
                    description=podcast.get("description"),
                    cover_url=podcast.get("cover_url"),
                    status=status,
                    rating=entry.score,
                    consumed_at=entry.date,
                ), podcast.get("genres", []), [podcast.get("author")] if podcast.get("author") else None

        return MediaCreate(
            title=entry.name,
            type=MediaType.PODCAST,
            external_url=entry.link,
            status=status,
            rating=entry.score,
            consumed_at=entry.date,
        ), None, [entry.author] if entry.author else None

    def _extract_youtube_id(self, url: str) -> str | None:
        """Extract YouTube video ID from URL."""
        patterns = [
            r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
            r"youtube\.com/v/([a-zA-Z0-9_-]{11})",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None


notion_importer = NotionImporter()
