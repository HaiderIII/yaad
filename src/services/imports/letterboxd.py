"""Letterboxd CSV import service."""

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
from src.services.metadata.tmdb import tmdb_service

logger = logging.getLogger(__name__)


def _extract_letterboxd_slug(uri: str | None) -> str | None:
    """Extract film slug from Letterboxd URI.

    Examples:
        'https://letterboxd.com/film/avatar-fire-and-ash/' -> 'avatar-fire-and-ash'
        'https://letterboxd.com/film/dune-part-two/' -> 'dune-part-two'
    """
    if not uri:
        return None
    match = re.search(r"/film/([^/]+)/?", uri)
    return match.group(1) if match else None


@dataclass
class LetterboxdEntry:
    """Parsed Letterboxd entry."""

    name: str
    year: int | None
    rating: float | None  # Letterboxd uses 0.5-5 scale
    watched_date: datetime | None
    letterboxd_uri: str | None
    rewatch: bool = False
    tags: list[str] | None = None


@dataclass
class ImportResult:
    """Result of an import operation."""

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] | None = None


class LetterboxdImporter:
    """Import films from Letterboxd CSV exports."""

    def parse_csv(self, content: str, file_type: str = "diary") -> list[LetterboxdEntry]:
        """Parse Letterboxd CSV content.

        Args:
            content: CSV file content as string
            file_type: 'diary' or 'watched'

        Returns:
            List of parsed entries
        """
        entries = []
        reader = csv.DictReader(io.StringIO(content))

        for row in reader:
            try:
                # Parse year
                year_str = row.get("Year", "").strip()
                year = int(year_str) if year_str else None

                # Parse rating (Letterboxd uses 0.5-5 scale)
                rating_str = row.get("Rating", "").strip()
                rating = float(rating_str) if rating_str else None

                # Parse watched date
                watched_date = None
                date_str = row.get("Watched Date", "") or row.get("Date", "")
                if date_str.strip():
                    try:
                        watched_date = datetime.strptime(date_str.strip(), "%Y-%m-%d")
                    except ValueError:
                        pass

                # Parse rewatch flag
                rewatch_str = row.get("Rewatch", "").strip().lower()
                rewatch = rewatch_str in ("yes", "true", "1")

                # Parse tags
                tags_str = row.get("Tags", "").strip()
                tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

                entry = LetterboxdEntry(
                    name=row.get("Name", "").strip(),
                    year=year,
                    rating=rating,
                    watched_date=watched_date,
                    letterboxd_uri=row.get("Letterboxd URI", "").strip() or None,
                    rewatch=rewatch,
                    tags=tags,
                )

                if entry.name:
                    entries.append(entry)

            except (ValueError, KeyError) as e:
                logger.warning(f"Failed to parse row: {row}, error: {e}")
                continue

        return entries

    async def import_entries(
        self,
        db: AsyncSession,
        user_id: int,
        entries: list[LetterboxdEntry],
        skip_existing: bool = True,
        fetch_metadata: bool = True,
    ) -> ImportResult:
        """Import parsed entries into the database.

        Args:
            db: Database session
            user_id: User ID to import for
            entries: List of parsed Letterboxd entries
            skip_existing: Skip films that already exist (by title+year or external_id)
            fetch_metadata: Fetch full metadata from TMDB

        Returns:
            Import result with counts
        """
        from sqlalchemy import or_
        from sqlalchemy.exc import IntegrityError

        result = ImportResult(errors=[])

        for entry in entries:
            try:
                # Fetch metadata from TMDB first (to get external_id for dedup check)
                build_result = await self._build_media_data(entry, fetch_metadata)
                media_data, genres, directors = build_result

                if media_data is None:
                    result.failed += 1
                    result.errors.append(f"Could not find: {entry.name} ({entry.year})")
                    continue

                # Check if already exists (by title+year OR by external_id)
                if skip_existing:
                    conditions = [
                        (Media.title.ilike(entry.name))
                        & (Media.year == entry.year if entry.year else True),
                    ]
                    # Also check by external_id if we have one
                    if media_data.external_id:
                        conditions.append(Media.external_id == media_data.external_id)

                    existing = await db.execute(
                        select(Media).where(
                            Media.user_id == user_id,
                            Media.type == MediaType.FILM,
                            or_(*conditions),
                        )
                    )
                    if existing.scalar_one_or_none():
                        result.skipped += 1
                        continue

                # Create media entry
                await create_media(
                    db=db,
                    user_id=user_id,
                    data=media_data,
                    genres=genres,
                    authors=directors,
                )
                # Flush to detect constraint violations early
                await db.flush()

                result.imported += 1

            except IntegrityError as e:
                # Duplicate key - rollback and continue
                await db.rollback()
                logger.warning(f"Duplicate entry skipped: {entry.name} - {e}")
                result.skipped += 1
            except Exception as e:
                # Other errors - rollback and continue
                await db.rollback()
                logger.exception(f"Failed to import: {entry.name}")
                result.failed += 1
                result.errors.append(f"Error importing {entry.name}: {str(e)}")

        # Final commit (may be empty if all were committed individually)
        try:
            await db.commit()
        except Exception:
            await db.rollback()

        return result

    async def import_single_entry(
        self,
        db: AsyncSession,
        user_id: int,
        entry: LetterboxdEntry,
        skip_existing: bool = True,
        fetch_metadata: bool = True,
    ) -> tuple[str, str | None]:
        """Import a single entry into the database.

        Args:
            db: Database session
            user_id: User ID to import for
            entry: Single Letterboxd entry
            skip_existing: Skip if already exists
            fetch_metadata: Fetch metadata from TMDB

        Returns:
            Tuple of (status, error_message) where status is 'imported', 'skipped', or 'failed'
        """
        from sqlalchemy import or_
        from sqlalchemy.exc import IntegrityError

        try:
            # Fetch metadata from TMDB first (to get external_id for dedup check)
            build_result = await self._build_media_data(entry, fetch_metadata)
            media_data, genres, directors = build_result

            if media_data is None:
                return ("failed", f"Could not find on TMDB: {entry.name} ({entry.year})")

            # Check if already exists (by title+year OR by external_id)
            if skip_existing:
                conditions = [
                    (Media.title.ilike(entry.name))
                    & (Media.year == entry.year if entry.year else True),
                ]
                # Also check by external_id if we have one
                if media_data.external_id:
                    conditions.append(Media.external_id == media_data.external_id)

                existing = await db.execute(
                    select(Media).where(
                        Media.user_id == user_id,
                        Media.type == MediaType.FILM,
                        or_(*conditions),
                    )
                )
                if existing.scalar_one_or_none():
                    return ("skipped", None)

            # Create media entry
            await create_media(
                db=db,
                user_id=user_id,
                data=media_data,
                genres=genres,
                authors=directors,
            )
            # Flush to detect constraint violations early
            await db.flush()

            return ("imported", None)

        except IntegrityError as e:
            # Duplicate key - rollback and continue
            await db.rollback()
            logger.warning(f"Duplicate entry skipped: {entry.name} - {e}")
            return ("skipped", None)
        except Exception as e:
            # Other errors - rollback and continue
            await db.rollback()
            logger.exception(f"Failed to import: {entry.name}")
            return ("failed", f"{entry.name}: {str(e)}")

    async def _build_media_data(
        self,
        entry: LetterboxdEntry,
        fetch_metadata: bool,
    ) -> tuple[MediaCreate | None, list[str] | None, list[str] | None]:
        """Build MediaCreate from Letterboxd entry, optionally fetching TMDB metadata.

        Letterboxd treats TV series as films, so we search both movies and TV
        on TMDB to find the best match.

        Returns:
            Tuple of (MediaCreate, genres, directors/creators)
        """
        # Determine status based on rating/watched date
        if entry.rating or entry.watched_date:
            status = MediaStatus.FINISHED
        else:
            status = MediaStatus.TO_CONSUME

        # Extract Letterboxd slug from URI for later use (friends ratings feature)
        letterboxd_slug = _extract_letterboxd_slug(entry.letterboxd_uri)

        if fetch_metadata:
            # Search TMDB for movies first
            movie_results = await tmdb_service.search_movies(entry.name, year=entry.year)

            # Also search TV series
            tv_results = await tmdb_service.search_tv(entry.name, year=entry.year)

            # Determine which result is better
            best_match, is_tv = self._pick_best_match(
                entry.name, entry.year, movie_results, tv_results
            )

            if best_match:
                tmdb_id = best_match["id"]

                if is_tv:
                    # It's a TV series
                    details = await tmdb_service.get_tv_details(tmdb_id)
                    if details:
                        genres = details.get("genres", [])
                        creators = [c["name"] for c in details.get("creators", [])]

                        media_data = MediaCreate(
                            title=details.get("original_title") or details.get("title") or entry.name,
                            local_title=details.get("local_title"),
                            type=MediaType.SERIES,
                            external_id=str(tmdb_id),
                            year=int(details.get("year")) if details.get("year") else entry.year,
                            description=details.get("description"),
                            cover_url=details.get("cover_url"),
                            status=status,
                            rating=entry.rating,
                            consumed_at=entry.watched_date,
                            # Extended TMDB metadata
                            tmdb_rating=details.get("tmdb_rating"),
                            tmdb_vote_count=details.get("tmdb_vote_count"),
                            popularity=details.get("popularity"),
                            original_language=details.get("original_language"),
                            tagline=details.get("tagline"),
                            cast=details.get("cast"),
                            keywords=details.get("keywords"),
                            certification=details.get("certification"),
                            # Series-specific
                            number_of_seasons=details.get("number_of_seasons"),
                            number_of_episodes=details.get("number_of_episodes"),
                            series_status=details.get("series_status"),
                            networks=details.get("networks"),
                            # Letterboxd slug for friends ratings (still useful for link)
                            letterboxd_slug=letterboxd_slug,
                        )
                        return media_data, genres, creators
                else:
                    # It's a movie
                    details = await tmdb_service.get_movie_details(tmdb_id)
                    if details:
                        genres = details.get("genres", [])
                        directors = [d["name"] for d in details.get("directors", [])]

                        media_data = MediaCreate(
                            title=details.get("original_title") or details.get("title") or entry.name,
                            local_title=details.get("local_title"),
                            type=MediaType.FILM,
                            external_id=str(tmdb_id),
                            year=int(details.get("year")) if details.get("year") else entry.year,
                            duration_minutes=details.get("duration_minutes"),
                            description=details.get("description"),
                            cover_url=details.get("cover_url"),
                            status=status,
                            rating=entry.rating,
                            consumed_at=entry.watched_date,
                            # Extended TMDB metadata
                            tmdb_rating=details.get("tmdb_rating"),
                            tmdb_vote_count=details.get("tmdb_vote_count"),
                            popularity=details.get("popularity"),
                            budget=details.get("budget"),
                            revenue=details.get("revenue"),
                            original_language=details.get("original_language"),
                            tagline=details.get("tagline"),
                            production_countries=details.get("production_countries"),
                            cast=details.get("cast"),
                            keywords=details.get("keywords"),
                            collection_id=details.get("collection_id"),
                            collection_name=details.get("collection_name"),
                            certification=details.get("certification"),
                            # Letterboxd slug for friends ratings
                            letterboxd_slug=letterboxd_slug,
                        )
                        return media_data, genres, directors

        # Fallback: create basic entry without metadata (assume film)
        return MediaCreate(
            title=entry.name,
            type=MediaType.FILM,
            year=entry.year,
            status=status,
            rating=entry.rating,
            consumed_at=entry.watched_date,
            letterboxd_slug=letterboxd_slug,
        ), None, None

    def _pick_best_match(
        self,
        title: str,
        year: int | None,
        movie_results: list[dict],
        tv_results: list[dict],
    ) -> tuple[dict | None, bool]:
        """Pick the best match between movie and TV results.

        Strategy:
        1. If only one has results, use that
        2. If both have results, compare title similarity and year match
        3. Prefer exact title matches
        4. If movie has no result or poor match but TV has good match, prefer TV

        Returns:
            Tuple of (best_result, is_tv) where is_tv is True if it's a TV series
        """
        title_lower = title.lower().strip()

        def score_match(result: dict) -> float:
            """Score a result based on title similarity and year match."""
            score = 0.0
            result_title = (result.get("title") or result.get("original_title") or "").lower()
            result_local = (result.get("local_title") or "").lower()

            # Exact title match (high priority)
            if result_title == title_lower or result_local == title_lower:
                score += 100
            # Title contains the search term
            elif title_lower in result_title or title_lower in result_local:
                score += 50
            # Search term contains the result title
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

            # Popularity bonus (TMDB vote_average as proxy)
            vote = result.get("vote_average", 0) or 0
            score += vote * 2

            return score

        # Score best movie and TV results
        best_movie = movie_results[0] if movie_results else None
        best_tv = tv_results[0] if tv_results else None

        movie_score = score_match(best_movie) if best_movie else -1
        tv_score = score_match(best_tv) if best_tv else -1

        # Log for debugging
        logger.debug(
            f"Matching '{title}' ({year}): movie_score={movie_score:.1f}, tv_score={tv_score:.1f}"
        )

        # If no results at all
        if movie_score < 0 and tv_score < 0:
            return None, False

        # If only one has results
        if movie_score < 0:
            return best_tv, True
        if tv_score < 0:
            return best_movie, False

        # Both have results - compare scores
        # Give slight preference to movies since Letterboxd is primarily a movie platform
        # TV needs to score significantly higher to win
        if tv_score > movie_score + 20:
            return best_tv, True
        else:
            return best_movie, False


letterboxd_importer = LetterboxdImporter()
