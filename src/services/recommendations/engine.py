"""Recommendation engine for generating personalized recommendations."""

import logging
import math
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.media import Media, MediaStatus, MediaType
from src.models.recommendation import Recommendation
from src.models.user import User
from src.services.metadata.books import book_service
from src.services.metadata.tmdb import tmdb_service
from src.services.recommendations.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU cache with max size to prevent unbounded memory growth."""

    def __init__(self, max_size: int = 500):
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Any | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)  # Remove oldest
        self._cache[key] = value

    def clear(self) -> None:
        self._cache.clear()


@dataclass
class ProgressEvent:
    """Progress event for SSE streaming."""

    progress: int  # 0-100
    status: str
    step: str  # profile, films, series, books, youtube, done
    count: int = 0  # items generated so far


class RecommendationEngine:
    """Engine for generating personalized media recommendations.

    Strategy:
    1. Analyze user's rated media to find preferred genres
    2. For each preferred genre: fetch similar content (5 per genre max)
    3. Also use "similar movies" from TMDB based on user's favorites
    4. Score by user taste alignment + quality signals
    5. Apply negative signals from dismissed content
    """

    RECOMMENDATIONS_PER_GENRE = 5
    MAX_PREFERRED_GENRES = 8  # Max preferred genres to prioritize
    MAX_TOTAL_GENRES = 12  # Total genres to fill (preferred + popular)
    SIMILAR_PER_SEED = 3  # Similar movies per user favorite
    MAX_SEEDS = 8  # Max user favorites to use as seeds
    MIN_RATING_FOR_SEED = 4
    STREAMING_COUNTRY = "FR"  # Country for streaming availability check

    # All TMDB movie genres for fallback
    ALL_MOVIE_GENRES = [
        ("Action", 28), ("Adventure", 12), ("Animation", 16), ("Comedy", 35),
        ("Crime", 80), ("Documentary", 99), ("Drama", 18), ("Family", 10751),
        ("Fantasy", 14), ("History", 36), ("Horror", 27), ("Music", 10402),
        ("Mystery", 9648), ("Romance", 10749), ("Science Fiction", 878),
        ("Thriller", 53), ("War", 10752), ("Western", 37),
    ]
    ALL_TV_GENRES = [
        ("Action & Adventure", 10759), ("Animation", 16), ("Comedy", 35),
        ("Crime", 80), ("Documentary", 99), ("Drama", 18), ("Family", 10751),
        ("Kids", 10762), ("Mystery", 9648), ("Sci-Fi & Fantasy", 10765),
        ("War & Politics", 10768), ("Western", 37),
    ]

    def __init__(self, db: AsyncSession):
        self.db = db
        self.embedding_service = EmbeddingService()
        self._user_profile_embedding = None
        self._user_genre_scores: dict[str, float] = {}  # genre -> avg rating (0-1)
        self._dismissed_embeddings: list[list[float]] = []
        # LRU cache with max 500 entries to prevent unbounded memory growth on 6GB servers
        self._streaming_cache: LRUCache = LRUCache(max_size=500)
        # Used during completion mode to track existing genre counts
        self._completion_genre_counts: dict[str, int] = {}
        self._completion_existing_ids: set[str] = set()

    async def generate_recommendations_for_user(
        self,
        user: User,
        force_refresh: bool = False,
    ) -> dict[MediaType, list[Recommendation]]:
        """Generate recommendations for all media types."""
        if not force_refresh:
            recent_cutoff = datetime.utcnow() - timedelta(hours=12)
            recent_count = await self.db.scalar(
                select(func.count(Recommendation.id)).where(
                    and_(
                        Recommendation.user_id == user.id,
                        Recommendation.generated_at > recent_cutoff,
                        Recommendation.is_dismissed == False,
                    )
                )
            )
            if recent_count and recent_count > 20:
                logger.info(f"User {user.id} has {recent_count} recent recommendations, returning existing")
                return await self._get_existing_recommendations(user.id)

        logger.info(f"Generating fresh recommendations for user {user.id}")

        try:
            # Build user taste profile
            await self._build_user_profile(user.id)

            # Get dismissed content BEFORE deleting anything
            dismissed_result = await self.db.execute(
                select(Recommendation.external_id, Recommendation.media_type, Recommendation.description).where(
                    and_(
                        Recommendation.user_id == user.id,
                        Recommendation.is_dismissed == True,
                    )
                )
            )
            dismissed_rows = dismissed_result.fetchall()
            dismissed_ids = {(row.external_id, row.media_type) for row in dismissed_rows}
            await self._build_dismissed_profile(dismissed_rows)

            # Generate new recommendations BEFORE deleting old ones (transaction safety)
            results = {}
            all_new_recommendations: list[Recommendation] = []
            generation_failed = False

            for media_type in [MediaType.FILM, MediaType.SERIES, MediaType.BOOK, MediaType.YOUTUBE]:
                try:
                    recommendations = await self._generate_for_type(user, media_type, dismissed_ids)
                    results[media_type] = recommendations
                    all_new_recommendations.extend(recommendations)
                    logger.info(f"Generated {len(recommendations)} {media_type.value} recommendations")
                except Exception as e:
                    logger.error(f"Error generating {media_type.value} recommendations: {e}", exc_info=True)
                    results[media_type] = []
                    generation_failed = True

            # Only delete old recommendations if we have new ones or at least some generation succeeded
            total_new = len(all_new_recommendations)
            if total_new > 0 or not generation_failed:
                # Clear old dismissed recommendations (older than 7 days)
                week_ago = datetime.utcnow() - timedelta(days=7)
                await self.db.execute(
                    Recommendation.__table__.delete().where(
                        and_(
                            Recommendation.user_id == user.id,
                            Recommendation.is_dismissed == True,
                            Recommendation.generated_at < week_ago,
                        )
                    )
                )
                # Clear old non-dismissed recommendations
                await self.db.execute(
                    Recommendation.__table__.delete().where(
                        and_(
                            Recommendation.user_id == user.id,
                            Recommendation.is_dismissed == False,
                        )
                    )
                )

                await self.db.commit()
                logger.info(f"Saved {total_new} new recommendations for user {user.id}")
            else:
                # Generation completely failed - keep old recommendations
                await self.db.rollback()
                logger.warning(f"Generation failed for user {user.id}, keeping existing recommendations")
                return await self._get_existing_recommendations(user.id)

            return results

        except Exception as e:
            # Ensure rollback on any unexpected error
            await self.db.rollback()
            logger.error(f"Critical error during recommendation generation: {e}", exc_info=True)
            raise

        finally:
            # Always clear cache to prevent memory leaks
            self._user_profile_embedding = None
            self._user_genre_scores = {}
            self._dismissed_embeddings = []
            self._streaming_cache.clear()

    async def generate_recommendations_streaming(
        self,
        user: User,
    ) -> AsyncIterator[ProgressEvent]:
        """Generate recommendations with streaming progress updates.

        Yields ProgressEvent objects for real-time UI updates.
        """
        logger.info(f"Starting streaming recommendation generation for user {user.id}")
        total_count = 0

        try:
            # Step 1: Build user taste profile (10%)
            yield ProgressEvent(5, "Building your taste profile...", "profile")
            await self._build_user_profile(user.id)
            yield ProgressEvent(10, "Profile built!", "profile")

            # Get dismissed content
            dismissed_result = await self.db.execute(
                select(Recommendation.external_id, Recommendation.media_type, Recommendation.description).where(
                    and_(
                        Recommendation.user_id == user.id,
                        Recommendation.is_dismissed == True,
                    )
                )
            )
            dismissed_rows = dismissed_result.fetchall()
            dismissed_ids = {(row.external_id, row.media_type) for row in dismissed_rows}
            await self._build_dismissed_profile(dismissed_rows)

            results = {}
            all_new_recommendations: list[Recommendation] = []

            # Step 2: Generate films (10-35%)
            yield ProgressEvent(15, "Finding films based on your favorites...", "films")
            try:
                film_recs = await self._generate_for_type(user, MediaType.FILM, dismissed_ids)
                results[MediaType.FILM] = film_recs
                all_new_recommendations.extend(film_recs)
                total_count += len(film_recs)
                yield ProgressEvent(35, f"Found {len(film_recs)} films!", "films", total_count)
            except Exception as e:
                logger.error(f"Error generating film recommendations: {e}", exc_info=True)
                results[MediaType.FILM] = []
                yield ProgressEvent(35, "Films complete (with errors)", "films", total_count)

            # Step 3: Generate series (35-55%)
            yield ProgressEvent(40, "Discovering series you might love...", "series")
            try:
                series_recs = await self._generate_for_type(user, MediaType.SERIES, dismissed_ids)
                results[MediaType.SERIES] = series_recs
                all_new_recommendations.extend(series_recs)
                total_count += len(series_recs)
                yield ProgressEvent(55, f"Found {len(series_recs)} series!", "series", total_count)
            except Exception as e:
                logger.error(f"Error generating series recommendations: {e}", exc_info=True)
                results[MediaType.SERIES] = []
                yield ProgressEvent(55, "Series complete (with errors)", "series", total_count)

            # Step 4: Generate books (55-80%)
            yield ProgressEvent(60, "Searching for books in your genres...", "books")
            try:
                book_recs = await self._generate_for_type(user, MediaType.BOOK, dismissed_ids)
                results[MediaType.BOOK] = book_recs
                all_new_recommendations.extend(book_recs)
                total_count += len(book_recs)
                yield ProgressEvent(80, f"Found {len(book_recs)} books!", "books", total_count)
            except Exception as e:
                logger.error(f"Error generating book recommendations: {e}", exc_info=True)
                results[MediaType.BOOK] = []
                yield ProgressEvent(80, "Books complete (with errors)", "books", total_count)

            # Step 5: Generate YouTube (80-90%)
            yield ProgressEvent(82, "Checking YouTube favorites...", "youtube")
            try:
                yt_recs = await self._generate_for_type(user, MediaType.YOUTUBE, dismissed_ids)
                results[MediaType.YOUTUBE] = yt_recs
                all_new_recommendations.extend(yt_recs)
                total_count += len(yt_recs)
                yield ProgressEvent(90, f"Found {len(yt_recs)} videos!", "youtube", total_count)
            except Exception as e:
                logger.error(f"Error generating YouTube recommendations: {e}", exc_info=True)
                results[MediaType.YOUTUBE] = []
                yield ProgressEvent(90, "YouTube complete (with errors)", "youtube", total_count)

            # Step 6: Save to database (90-100%)
            yield ProgressEvent(92, "Saving recommendations...", "saving", total_count)

            if all_new_recommendations:
                # Clear old recommendations
                week_ago = datetime.utcnow() - timedelta(days=7)
                await self.db.execute(
                    Recommendation.__table__.delete().where(
                        and_(
                            Recommendation.user_id == user.id,
                            Recommendation.is_dismissed == True,
                            Recommendation.generated_at < week_ago,
                        )
                    )
                )
                await self.db.execute(
                    Recommendation.__table__.delete().where(
                        and_(
                            Recommendation.user_id == user.id,
                            Recommendation.is_dismissed == False,
                        )
                    )
                )
                # Commit BEFORE sending done event to prevent client disconnect issues
                await self.db.commit()
                logger.info(f"Saved {total_count} new recommendations for user {user.id}")

            # Send done event AFTER commit is complete
            yield ProgressEvent(100, f"Done! Generated {total_count} recommendations", "done", total_count)

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Critical error during streaming generation: {e}", exc_info=True)
            yield ProgressEvent(100, f"Error: {str(e)}", "error", total_count)

        finally:
            self._user_profile_embedding = None
            self._user_genre_scores = {}
            self._dismissed_embeddings = []
            self._streaming_cache.clear()

    async def complete_recommendations_streaming(
        self,
        user: User,
    ) -> AsyncIterator[ProgressEvent]:
        """Complete existing recommendations by filling gaps (genres with < 5 items).

        Unlike full refresh, this keeps existing recommendations and only adds missing ones.
        Yields ProgressEvent objects for real-time UI updates.
        """
        logger.info(f"Starting streaming recommendation completion for user {user.id}")
        total_count = 0

        try:
            # Step 1: Build user taste profile (10%)
            yield ProgressEvent(5, "Building your taste profile...", "profile")
            await self._build_user_profile(user.id)
            yield ProgressEvent(10, "Profile built!", "profile")

            # Get existing recommendations and count per genre per type
            existing_result = await self.db.execute(
                select(Recommendation).where(
                    and_(
                        Recommendation.user_id == user.id,
                        Recommendation.is_dismissed == False,
                        Recommendation.added_to_library == False,
                    )
                )
            )
            existing_recs = existing_result.scalars().all()

            # Build map of existing counts: {(media_type, genre_name): count}
            existing_genre_counts: dict[tuple[MediaType, str], int] = defaultdict(int)
            existing_external_ids: set[str] = set()
            for rec in existing_recs:
                genre = rec.genre_name or "Découvertes"
                existing_genre_counts[(rec.media_type, genre)] += 1
                existing_external_ids.add(rec.external_id)

            # Get dismissed content
            dismissed_result = await self.db.execute(
                select(Recommendation.external_id, Recommendation.media_type, Recommendation.description).where(
                    and_(
                        Recommendation.user_id == user.id,
                        Recommendation.is_dismissed == True,
                    )
                )
            )
            dismissed_rows = dismissed_result.fetchall()
            dismissed_ids = {(row.external_id, row.media_type) for row in dismissed_rows}
            await self._build_dismissed_profile(dismissed_rows)

            # Check which types need completion
            types_to_complete = []
            for media_type in [MediaType.FILM, MediaType.SERIES, MediaType.BOOK, MediaType.YOUTUBE]:
                type_genres = {
                    genre: count
                    for (mt, genre), count in existing_genre_counts.items()
                    if mt == media_type
                }
                needs_more = any(count < self.RECOMMENDATIONS_PER_GENRE for count in type_genres.values())
                has_none = not type_genres  # No recommendations at all for this type
                if needs_more or has_none:
                    types_to_complete.append(media_type)

            if not types_to_complete:
                yield ProgressEvent(100, "All recommendations are already complete!", "done", len(existing_recs))
                return

            all_new_recommendations: list[Recommendation] = []

            # Progress mapping for each type
            type_progress = {
                MediaType.FILM: (15, 35, "Finding more films...", "films"),
                MediaType.SERIES: (40, 55, "Discovering more series...", "series"),
                MediaType.BOOK: (60, 80, "Searching for more books...", "books"),
                MediaType.YOUTUBE: (82, 90, "Checking for more videos...", "youtube"),
            }

            for media_type in [MediaType.FILM, MediaType.SERIES, MediaType.BOOK, MediaType.YOUTUBE]:
                start_pct, end_pct, msg, step = type_progress[media_type]

                if media_type not in types_to_complete:
                    yield ProgressEvent(end_pct, f"{step.capitalize()} already complete", step, total_count)
                    continue

                yield ProgressEvent(start_pct, msg, step)
                try:
                    # Pass existing genre counts so _generate_for_type_completing knows what to skip
                    self._completion_genre_counts = {
                        genre: count
                        for (mt, genre), count in existing_genre_counts.items()
                        if mt == media_type
                    }
                    self._completion_existing_ids = existing_external_ids

                    recs = await self._generate_for_type(user, media_type, dismissed_ids)

                    # Filter out recommendations that already exist
                    new_recs = [r for r in recs if r.external_id not in existing_external_ids]
                    all_new_recommendations.extend(new_recs)
                    total_count += len(new_recs)
                    yield ProgressEvent(end_pct, f"Found {len(new_recs)} new {step}!", step, total_count)
                except Exception as e:
                    logger.error(f"Error completing {media_type.value} recommendations: {e}", exc_info=True)
                    yield ProgressEvent(end_pct, f"{step.capitalize()} complete (with errors)", step, total_count)
                finally:
                    self._completion_genre_counts = {}
                    self._completion_existing_ids = set()

            # Step 6: Save to database (90-100%)
            yield ProgressEvent(92, "Saving new recommendations...", "saving", total_count)

            if all_new_recommendations:
                # Only clean old dismissed (no deletion of existing non-dismissed)
                week_ago = datetime.utcnow() - timedelta(days=7)
                await self.db.execute(
                    Recommendation.__table__.delete().where(
                        and_(
                            Recommendation.user_id == user.id,
                            Recommendation.is_dismissed == True,
                            Recommendation.generated_at < week_ago,
                        )
                    )
                )
                await self.db.commit()
                logger.info(f"Saved {total_count} new completion recommendations for user {user.id}")

            yield ProgressEvent(100, f"Done! Added {total_count} new recommendations", "done", total_count)

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Critical error during recommendation completion: {e}", exc_info=True)
            yield ProgressEvent(100, f"Error: {str(e)}", "error", total_count)

        finally:
            self._user_profile_embedding = None
            self._user_genre_scores = {}
            self._dismissed_embeddings = []
            self._streaming_cache.clear()
            self._completion_genre_counts = {}
            self._completion_existing_ids = set()

    async def _build_user_profile(self, user_id: int) -> None:
        """Build user taste profile from rated media."""
        result = await self.db.execute(
            select(Media)
            .options(selectinload(Media.genres))
            .where(and_(Media.user_id == user_id, Media.rating != None))
        )
        rated_media = list(result.scalars().all())

        if not rated_media:
            logger.info(f"User {user_id} has no rated media")
            return

        # Build embedding profile
        embeddings_with_ratings = [
            (m.embedding, m.rating) for m in rated_media if m.embedding and m.rating
        ]
        if embeddings_with_ratings:
            self._user_profile_embedding = self.embedding_service.compute_user_profile_embedding(
                embeddings_with_ratings
            )

        # Build genre scores: count and avg rating per genre
        genre_data: dict[str, list[float]] = defaultdict(list)
        for media in rated_media:
            if media.rating and media.genres:
                normalized_rating = (media.rating - 1) / 4  # 0-1
                for genre in media.genres:
                    genre_data[genre.name].append(normalized_rating)

        # Calculate score = avg_rating * sqrt(count) to favor both quality and frequency
        for genre_name, ratings in genre_data.items():
            avg = sum(ratings) / len(ratings)
            count_factor = min(len(ratings) ** 0.5 / 3, 1.0)  # sqrt(count)/3, capped at 1
            self._user_genre_scores[genre_name] = avg * 0.7 + count_factor * 0.3

        # Log top genres
        top_genres = sorted(self._user_genre_scores.items(), key=lambda x: x[1], reverse=True)[:10]
        logger.info(f"User {user_id} top genres: {[(g, f'{s:.2f}') for g, s in top_genres]}")

    async def _build_dismissed_profile(self, dismissed_rows: list) -> None:
        """Build profile of dismissed content."""
        dismissed_texts = [row.description[:300] for row in dismissed_rows if row.description]
        if len(dismissed_texts) >= 3:
            try:
                # Use async version to avoid blocking event loop
                self._dismissed_embeddings = await self.embedding_service.generate_embeddings_batch_async(dismissed_texts[:20])
            except Exception as e:
                logger.debug(f"Failed to build dismissed profile: {e}")

    async def _generate_for_type(
        self,
        user: User,
        media_type: MediaType,
        dismissed_ids: set[tuple[str, MediaType]],
    ) -> list[Recommendation]:
        """Generate recommendations for a media type."""
        user_media = await self._get_user_media(user.id, media_type)
        existing_ids = {m.external_id for m in user_media if m.external_id}
        dismissed_for_type = {ext_id for ext_id, mtype in dismissed_ids if mtype == media_type}
        existing_ids.update(dismissed_for_type)
        # In completion mode, also exclude already-recommended IDs
        if self._completion_existing_ids:
            existing_ids.update(self._completion_existing_ids)

        if media_type in [MediaType.FILM, MediaType.SERIES]:
            return await self._generate_film_series(user, media_type, user_media, existing_ids)
        elif media_type == MediaType.BOOK:
            return await self._generate_books(user, user_media, existing_ids)
        elif media_type == MediaType.YOUTUBE:
            return await self._generate_youtube(user, user_media, existing_ids)
        return []

    async def _generate_film_series(
        self,
        user: User,
        media_type: MediaType,
        user_media: list[Media],
        existing_ids: set[str],
    ) -> list[Recommendation]:
        """Generate film/series recommendations based on user's preferred genres."""
        tmdb_type = "movie" if media_type == MediaType.FILM else "tv"
        recommendations: list[Recommendation] = []
        seen_ids: set[int] = set()
        genre_counts: dict[str, int] = defaultdict(int)

        # In completion mode, pre-seed genre counts from existing recommendations
        if self._completion_genre_counts:
            for genre, count in self._completion_genre_counts.items():
                genre_counts[genre] = count

        # === STEP 1: Get user's preferred genres sorted by score ===
        user_genres = sorted(
            self._user_genre_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        # Map to TMDB genre IDs
        preferred_genres: list[tuple[str, int, float]] = []  # (name, tmdb_id, score)
        for genre_name, score in user_genres:
            tmdb_id = self._get_tmdb_genre_id(genre_name, tmdb_type)
            if tmdb_id:
                preferred_genres.append((genre_name, tmdb_id, score))

        logger.info(f"User preferred genres for {media_type.value}: {[(g, f'{s:.2f}') for g, _, s in preferred_genres[:8]]}")

        # === STEP 2: Similar movies from user's highly-rated content ===
        highly_rated = sorted(
            [m for m in user_media if m.rating and m.rating >= self.MIN_RATING_FOR_SEED and m.external_id],
            key=lambda x: (x.rating or 0),
            reverse=True
        )[:self.MAX_SEEDS]

        similar_candidates: list[dict] = []
        for seed in highly_rated:
            try:
                similar = await tmdb_service.get_similar(tmdb_type, int(seed.external_id))
                for item in similar[:self.SIMILAR_PER_SEED]:
                    if item["id"] not in seen_ids and str(item["id"]) not in existing_ids:
                        # Determine genre from item's genre_ids
                        item_genre = self._get_primary_genre(item.get("genre_ids", []), tmdb_type)
                        item["source"] = "similar"
                        item["genre_name"] = item_genre or "Similar"
                        item["seed_title"] = seed.title
                        item["seed_rating"] = seed.rating
                        similar_candidates.append(item)
                        seen_ids.add(item["id"])
            except Exception as e:
                logger.debug(f"Failed to get similar for {seed.title}: {e}")

        # Score and add similar candidates (up to 5 per genre)
        scored_similar = await self._score_candidates(similar_candidates, media_type)
        # Enrich with streaming info
        scored_similar = await self._enrich_with_streaming(scored_similar, tmdb_type)
        for candidate in scored_similar:
            genre = candidate.get("genre_name", "Similar")
            if genre_counts[genre] >= self.RECOMMENDATIONS_PER_GENRE:
                continue

            rec = self._create_recommendation(user, media_type, candidate)
            self.db.add(rec)
            recommendations.append(rec)
            genre_counts[genre] += 1

        logger.info(f"Added {len(recommendations)} similar-based recommendations")

        # === STEP 3: Genre-based discovery for user's top genres ===
        # First pass: Fill genres from user preferences
        for genre_name, genre_id, genre_score in preferred_genres[:self.MAX_PREFERRED_GENRES]:
            if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                continue

            needed = self.RECOMMENDATIONS_PER_GENRE - genre_counts[genre_name]
            try:
                # Fetch high-quality content in this genre - get more to ensure we fill the quota
                discovered = await tmdb_service.discover(
                    tmdb_type,
                    with_genres=[genre_id],
                    vote_average_gte=6.5,  # Slightly lower threshold for more options
                    vote_count_gte=50,
                    sort_by="vote_average.desc",
                )

                genre_candidates = []
                for item in discovered:
                    if item["id"] not in seen_ids and str(item["id"]) not in existing_ids:
                        item["source"] = "genre_discover"
                        item["genre_name"] = genre_name
                        item["user_genre_score"] = genre_score
                        genre_candidates.append(item)
                        seen_ids.add(item["id"])
                        if len(genre_candidates) >= needed + 5:  # Fetch extra for scoring
                            break

                # Score and select top candidates
                scored = await self._score_candidates(genre_candidates, media_type)
                # Enrich with streaming info
                scored = await self._enrich_with_streaming(scored[:needed + 2], tmdb_type)
                for candidate in scored[:needed]:
                    rec = self._create_recommendation(user, media_type, candidate)
                    self.db.add(rec)
                    recommendations.append(rec)
                    genre_counts[genre_name] += 1

                logger.debug(f"Added {min(len(scored), needed)} recommendations for {genre_name}")

            except Exception as e:
                logger.warning(f"Failed to discover {genre_name}: {e}")

        # === STEP 4: Fill genres that came from similar movies to 5 each ===
        # Get all genres that have recommendations but less than 5
        all_genres = self.ALL_MOVIE_GENRES if tmdb_type == "movie" else self.ALL_TV_GENRES
        genres_to_fill = [
            (name, gid) for name, gid in all_genres
            if 0 < genre_counts[name] < self.RECOMMENDATIONS_PER_GENRE
        ]

        for genre_name, genre_id in genres_to_fill:
            needed = self.RECOMMENDATIONS_PER_GENRE - genre_counts[genre_name]
            logger.debug(f"Filling partial genre {genre_name} (have {genre_counts[genre_name]}, need {needed} more)")

            try:
                discovered = await tmdb_service.discover(
                    tmdb_type,
                    with_genres=[genre_id],
                    vote_average_gte=6.5,
                    vote_count_gte=50,
                    sort_by="vote_average.desc",
                )

                for item in discovered:
                    if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                        break
                    if item["id"] not in seen_ids and str(item["id"]) not in existing_ids:
                        item["source"] = "genre_discover"
                        item["genre_name"] = genre_name
                        item["user_genre_score"] = 0.5  # Default score for non-preferred
                        seen_ids.add(item["id"])

                        item["score"] = 0.65
                        enriched = await self._enrich_with_streaming([item], tmdb_type)
                        if enriched:
                            rec = self._create_recommendation(user, media_type, enriched[0])
                            self.db.add(rec)
                            recommendations.append(rec)
                            genre_counts[genre_name] += 1

            except Exception as e:
                logger.warning(f"Failed to fill genre {genre_name}: {e}")

        # === STEP 5: Second pass - Fill any preferred genres that didn't reach 5 ===
        for genre_name, genre_id, genre_score in preferred_genres[:self.MAX_PREFERRED_GENRES]:
            if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                continue

            needed = self.RECOMMENDATIONS_PER_GENRE - genre_counts[genre_name]
            logger.debug(f"Second pass: filling {genre_name} (need {needed} more)")

            try:
                # Try with even lower thresholds and popularity sort
                discovered = await tmdb_service.discover(
                    tmdb_type,
                    with_genres=[genre_id],
                    vote_average_gte=6.0,
                    vote_count_gte=20,
                    sort_by="popularity.desc",
                )

                for item in discovered:
                    if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                        break
                    if item["id"] not in seen_ids and str(item["id"]) not in existing_ids:
                        item["source"] = "genre_discover"
                        item["genre_name"] = genre_name
                        item["user_genre_score"] = genre_score
                        seen_ids.add(item["id"])

                        # Quick score and add
                        item["score"] = 0.6 + genre_score * 0.1
                        enriched = await self._enrich_with_streaming([item], tmdb_type)
                        if enriched:
                            rec = self._create_recommendation(user, media_type, enriched[0])
                            self.db.add(rec)
                            recommendations.append(rec)
                            genre_counts[genre_name] += 1

            except Exception as e:
                logger.warning(f"Second pass failed for {genre_name}: {e}")

        # Log final distribution
        logger.info(f"Final genre distribution: {dict(genre_counts)}")
        return recommendations

    # Curated lists of popular/acclaimed books by genre (titles for search)
    # These are recognized classics and bestsellers from Babelio, Goodreads, and literary awards
    CURATED_BOOKS = {
        "Science Fiction": [
            "Dune Frank Herbert", "Foundation Isaac Asimov", "Neuromancer William Gibson",
            "The Martian Andy Weir", "Ender's Game Orson Scott Card", "1984 George Orwell",
            "Brave New World Aldous Huxley", "The Left Hand of Darkness Ursula K Le Guin",
            "Hyperion Dan Simmons", "Snow Crash Neal Stephenson", "Project Hail Mary Andy Weir",
            "The Three-Body Problem Liu Cixin", "Fahrenheit 451 Ray Bradbury",
        ],
        "Fantasy": [
            "The Name of the Wind Patrick Rothfuss", "The Way of Kings Brandon Sanderson",
            "A Game of Thrones George R R Martin", "The Hobbit J R R Tolkien",
            "The Final Empire Brandon Sanderson", "Assassin's Apprentice Robin Hobb",
            "The Lies of Locke Lamora Scott Lynch", "The Blade Itself Joe Abercrombie",
            "Piranesi Susanna Clarke", "Circe Madeline Miller", "The House in the Cerulean Sea TJ Klune",
        ],
        "Mystery": [
            "The Girl with the Dragon Tattoo Stieg Larsson", "Gone Girl Gillian Flynn",
            "In the Woods Tana French", "The Silent Patient Alex Michaelides",
            "Big Little Lies Liane Moriarty", "The Da Vinci Code Dan Brown",
            "And Then There Were None Agatha Christie", "The Girl on the Train Paula Hawkins",
            "Sharp Objects Gillian Flynn", "The Thursday Murder Club Richard Osman",
        ],
        "Thriller": [
            "The Hunger Games Suzanne Collins", "The Shining Stephen King",
            "Gone Girl Gillian Flynn", "The Girl with the Dragon Tattoo Stieg Larsson",
            "The Bourne Identity Robert Ludlum", "Misery Stephen King",
            "Rebecca Daphne du Maurier", "The Silence of the Lambs Thomas Harris",
            "Dark Places Gillian Flynn", "Behind Closed Doors B A Paris",
        ],
        "Classic Literature": [
            "Pride and Prejudice Jane Austen", "1984 George Orwell", "To Kill a Mockingbird Harper Lee",
            "The Great Gatsby F Scott Fitzgerald", "Jane Eyre Charlotte Bronte",
            "Wuthering Heights Emily Bronte", "Crime and Punishment Fyodor Dostoevsky",
            "Anna Karenina Leo Tolstoy", "The Count of Monte Cristo Alexandre Dumas",
            "Les Misérables Victor Hugo", "Don Quixote Miguel de Cervantes",
        ],
        "Contemporary Fiction": [
            "The Kite Runner Khaled Hosseini", "A Little Life Hanya Yanagihara",
            "Normal People Sally Rooney", "Where the Crawdads Sing Delia Owens",
            "The Midnight Library Matt Haig", "Circe Madeline Miller",
            "A Man Called Ove Fredrik Backman", "Eleanor Oliphant Is Completely Fine Gail Honeyman",
            "The Seven Husbands of Evelyn Hugo Taylor Jenkins Reid", "Lessons in Chemistry Bonnie Garmus",
        ],
        "Philosophy": [
            "Meditations Marcus Aurelius", "The Stranger Albert Camus",
            "Man's Search for Meaning Viktor Frankl", "Being and Nothingness Jean-Paul Sartre",
            "Thus Spoke Zarathustra Friedrich Nietzsche", "The Republic Plato",
            "Critique of Pure Reason Immanuel Kant", "The Art of War Sun Tzu",
            "Letters from a Stoic Seneca", "The Consolation of Philosophy Boethius",
        ],
        "Psychology": [
            "Thinking Fast and Slow Daniel Kahneman", "The Power of Habit Charles Duhigg",
            "Atomic Habits James Clear", "The Body Keeps the Score Bessel van der Kolk",
            "Quiet Susan Cain", "Emotional Intelligence Daniel Goleman",
            "Man's Search for Meaning Viktor Frankl", "Flow Mihaly Csikszentmihalyi",
            "The Psychopath Test Jon Ronson", "Attached Amir Levine",
        ],
        "Biography": [
            "Steve Jobs Walter Isaacson", "Educated Tara Westover",
            "Becoming Michelle Obama", "The Diary of a Young Girl Anne Frank",
            "Long Walk to Freedom Nelson Mandela", "Einstein His Life and Universe Walter Isaacson",
            "Born a Crime Trevor Noah", "Shoe Dog Phil Knight",
            "A Promised Land Barack Obama", "The Glass Castle Jeannette Walls",
        ],
        "History": [
            "Sapiens Yuval Noah Harari", "Guns Germs and Steel Jared Diamond",
            "A People's History of the United States Howard Zinn", "The Silk Roads Peter Frankopan",
            "SPQR Mary Beard", "The Rise and Fall of the Third Reich William Shirer",
            "Team of Rivals Doris Kearns Goodwin", "1491 Charles Mann",
            "The Splendid and the Vile Erik Larson", "Stamped from the Beginning Ibram X Kendi",
        ],
        "Science": [
            "A Brief History of Time Stephen Hawking", "Cosmos Carl Sagan",
            "The Selfish Gene Richard Dawkins", "Silent Spring Rachel Carson",
            "The Origin of Species Charles Darwin", "Astrophysics for People in a Hurry Neil deGrasse Tyson",
            "The Immortal Life of Henrietta Lacks Rebecca Skloot", "Why We Sleep Matthew Walker",
            "The Gene Siddhartha Mukherjee", "Sapiens Yuval Noah Harari",
        ],
        "Horror": [
            "It Stephen King", "The Shining Stephen King", "Dracula Bram Stoker",
            "Frankenstein Mary Shelley", "House of Leaves Mark Z Danielewski",
            "The Haunting of Hill House Shirley Jackson", "Pet Sematary Stephen King",
            "Mexican Gothic Silvia Moreno-Garcia", "The Exorcist William Peter Blatty",
            "Bird Box Josh Malerman", "Hell House Richard Matheson",
        ],
    }

    async def _generate_books(
        self,
        user: User,
        user_media: list[Media],
        existing_ids: set[str],
    ) -> list[Recommendation]:
        """Generate book recommendations based on user preferences using curated lists."""
        recommendations: list[Recommendation] = []
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()  # Also track titles to avoid duplicates
        genre_counts: dict[str, int] = defaultdict(int)

        # In completion mode, pre-seed genre counts from existing recommendations
        if self._completion_genre_counts:
            for genre, count in self._completion_genre_counts.items():
                genre_counts[genre] = count

        # Get user's existing book titles (lowercase for comparison)
        user_book_titles = {m.title.lower() for m in user_media if m.title}

        # Get user's book genre preferences
        user_book_genres = set()
        for media in user_media:
            if media.rating and media.rating >= 4:
                for genre in media.genres:
                    user_book_genres.add(genre.name.lower())

        # All genres with curated books
        all_genres = list(self.CURATED_BOOKS.keys())

        # Prioritize user's preferred genres
        prioritized = []
        others = []
        for genre in all_genres:
            genre_lower = genre.lower()
            if any(ug in genre_lower or genre_lower in ug for ug in user_book_genres):
                prioritized.append(genre)
            else:
                others.append(genre)

        ordered_genres = prioritized + others

        for genre_name in ordered_genres[:self.MAX_TOTAL_GENRES]:
            if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                continue

            curated_titles = self.CURATED_BOOKS.get(genre_name, [])
            needed = self.RECOMMENDATIONS_PER_GENRE - genre_counts[genre_name]

            for book_query in curated_titles:
                if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                    break

                # Skip if user already has this book
                query_lower = book_query.lower()
                if any(t in query_lower or query_lower in t for t in user_book_titles):
                    continue

                try:
                    # Search for this specific book
                    results = await book_service.search_books(book_query, limit=3)

                    for book in results:
                        if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                            break

                        book_title = book.get("title", "").lower()

                        # Skip if already seen or user has it
                        if book_title in seen_titles:
                            continue
                        if any(t in book_title or book_title in t for t in user_book_titles):
                            continue

                        book_id = (
                            book.get("external_id") or book.get("isbn") or
                            book.get("open_library_key", "").split("/")[-1] or
                            book.get("key", "").split("/")[-1]
                        )

                        if book_id and book_id not in seen_ids and book_id not in existing_ids:
                            seen_ids.add(book_id)
                            seen_titles.add(book_title)

                            is_preferred = any(
                                ug in genre_name.lower() or genre_name.lower() in ug
                                for ug in user_book_genres
                            )

                            # Higher base score for curated books (they're recognized/popular)
                            base_score = 0.80 if is_preferred else 0.70

                            # Bonus for books with covers (better presentation)
                            if book.get("cover_url"):
                                base_score += 0.05

                            rec = Recommendation(
                                user_id=user.id,
                                media_type=MediaType.BOOK,
                                external_id=book_id,
                                title=book.get("title", "Unknown"),
                                year=book.get("year") or book.get("first_publish_year"),
                                cover_url=book.get("cover_url"),
                                description=book.get("description"),
                                score=min(base_score, 0.95),
                                source="curated" if is_preferred else "popular",
                                genre_name=genre_name,
                                generated_at=datetime.utcnow(),
                            )
                            self.db.add(rec)
                            recommendations.append(rec)
                            genre_counts[genre_name] += 1
                            break  # Found a book for this query, move to next

                except Exception as e:
                    logger.debug(f"Failed to search book '{book_query}': {e}")
                    continue

        # Second pass: Try to fill genres that didn't reach 5 using generic searches
        for genre_name in ordered_genres[:self.MAX_TOTAL_GENRES]:
            if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                continue

            needed = self.RECOMMENDATIONS_PER_GENRE - genre_counts[genre_name]
            logger.debug(f"Book second pass: filling {genre_name} (need {needed} more)")

            try:
                # Search for popular books in this genre
                results = await book_service.search_books(f"best {genre_name} books", limit=10)

                for book in results:
                    if genre_counts[genre_name] >= self.RECOMMENDATIONS_PER_GENRE:
                        break

                    book_title = book.get("title", "").lower()
                    if book_title in seen_titles:
                        continue
                    if any(t in book_title or book_title in t for t in user_book_titles):
                        continue

                    book_id = (
                        book.get("external_id") or book.get("isbn") or
                        book.get("open_library_key", "").split("/")[-1] or
                        book.get("key", "").split("/")[-1]
                    )

                    if book_id and book_id not in seen_ids and book_id not in existing_ids:
                        seen_ids.add(book_id)
                        seen_titles.add(book_title)

                        rec = Recommendation(
                            user_id=user.id,
                            media_type=MediaType.BOOK,
                            external_id=book_id,
                            title=book.get("title", "Unknown"),
                            year=book.get("year") or book.get("first_publish_year"),
                            cover_url=book.get("cover_url"),
                            description=book.get("description"),
                            score=0.65,  # Lower score for generic search results
                            source="popular",
                            genre_name=genre_name,
                            generated_at=datetime.utcnow(),
                        )
                        self.db.add(rec)
                        recommendations.append(rec)
                        genre_counts[genre_name] += 1

            except Exception as e:
                logger.debug(f"Book second pass failed for {genre_name}: {e}")

        logger.info(f"Book genre distribution: {dict(genre_counts)}")
        return recommendations

    async def _generate_youtube(
        self,
        user: User,
        user_media: list[Media],
        existing_ids: set[str],
    ) -> list[Recommendation]:
        """Generate YouTube recommendations from favorite channels."""
        recommendations: list[Recommendation] = []

        highly_rated = [
            m for m in user_media
            if m.rating and m.rating >= 4 and m.youtube_metadata and m.youtube_metadata.channel_name
        ]

        channel_data: dict[str, dict] = {}
        for media in highly_rated:
            channel = media.youtube_metadata.channel_name
            if channel not in channel_data:
                channel_data[channel] = {"count": 0, "total_rating": 0}
            channel_data[channel]["count"] += 1
            channel_data[channel]["total_rating"] += media.rating

        if not channel_data:
            return recommendations

        # Sort by engagement score
        sorted_channels = sorted(
            channel_data.items(),
            key=lambda x: (x[1]["total_rating"] / x[1]["count"]) * x[1]["count"],
            reverse=True
        )

        seen_ids: set[str] = set()
        for channel_name, stats in sorted_channels[:10]:
            channel_videos = [
                m for m in user_media
                if m.status == MediaStatus.TO_CONSUME
                and m.youtube_metadata and m.youtube_metadata.channel_name == channel_name
                and m.external_id and m.external_id not in seen_ids and m.external_id not in existing_ids
            ]

            avg_rating = stats["total_rating"] / stats["count"]
            for media in channel_videos[:self.RECOMMENDATIONS_PER_GENRE]:
                seen_ids.add(media.external_id)
                score = min(0.7 + (avg_rating - 4) * 0.1 + (stats["count"] * 0.02), 0.98)

                rec = Recommendation(
                    user_id=user.id,
                    media_type=MediaType.YOUTUBE,
                    external_id=media.external_id,
                    title=media.title,
                    year=media.year,
                    cover_url=media.cover_url,
                    description=media.description,
                    score=score,
                    source="favorite_channel",
                    genre_name=channel_name,
                    external_url=media.external_url,
                    generated_at=datetime.utcnow(),
                )
                self.db.add(rec)
                recommendations.append(rec)

        return recommendations

    async def _score_candidates(
        self,
        candidates: list[dict],
        media_type: MediaType,
    ) -> list[dict]:
        """Score and sort candidates using multiple signals.

        Scoring breakdown (max ~1.0):
        - Source quality: 0.25-0.40
        - TMDB rating: 0-0.20
        - Vote count reliability: 0-0.08
        - Popularity: 0-0.08
        - User genre preference: 0-0.15
        - Semantic similarity: 0-0.12
        - Recency bonus (recent but not too new): 0-0.05
        - Dismissed penalty: -0.20 to 0
        """
        if not candidates:
            return []

        current_year = datetime.utcnow().year

        source_weights = {
            "similar": 0.40,
            "preferred_genre": 0.35,
            "genre_discover": 0.25,
        }

        # Batch generate embeddings for all candidates with descriptions
        # This is much more efficient than generating one at a time
        candidate_texts = []
        candidate_indices = []
        for i, candidate in enumerate(candidates):
            if self._user_profile_embedding and candidate.get("overview"):
                text = self.embedding_service.create_media_text(
                    title=candidate.get("title", ""),
                    description=candidate.get("overview"),
                    year=candidate.get("year"),
                )
                candidate_texts.append(text)
                candidate_indices.append(i)

        # Generate all embeddings in one batch (async to avoid blocking)
        candidate_embeddings: list[list[float]] = []
        if candidate_texts:
            try:
                candidate_embeddings = await self.embedding_service.generate_embeddings_batch_async(candidate_texts)
            except Exception as e:
                logger.debug(f"Failed to generate batch embeddings: {e}")
                candidate_embeddings = []

        # Map embeddings back to candidates
        embedding_map: dict[int, list[float]] = {}
        for idx, emb in zip(candidate_indices, candidate_embeddings):
            embedding_map[idx] = emb

        for i, candidate in enumerate(candidates):
            score = 0.0
            source = candidate.get("source", "genre_discover")
            score += source_weights.get(source, 0.20)

            # Bonus for similar from highly-rated seed
            if source == "similar" and candidate.get("seed_rating"):
                score += (candidate["seed_rating"] - 4) * 0.05

            # TMDB rating bonus (0-0.20)
            vote_avg = candidate.get("vote_average", 0)
            if vote_avg:
                rating_bonus = max(0, (vote_avg - 5) / 5) * 0.20
                score += rating_bonus

            # Vote count reliability bonus (more votes = more reliable rating)
            vote_count = candidate.get("vote_count", 0)
            if vote_count:
                # Log scale: 100 votes = 0.04, 1000 votes = 0.06, 10000 votes = 0.08
                reliability = min(math.log10(max(vote_count, 1)) / 5, 1.0) * 0.08
                score += reliability

            # Popularity bonus (capped at 0.08)
            if candidate.get("popularity"):
                pop_bonus = min(candidate["popularity"] / 500, 1.0) * 0.08
                score += pop_bonus

            # User genre preference bonus
            genre = candidate.get("genre_name", "")
            if genre and genre in self._user_genre_scores:
                score += self._user_genre_scores[genre] * 0.15

            # Recency bonus: prefer films from last 10 years but not too new
            year = candidate.get("year")
            if year:
                try:
                    year_int = int(year)
                    years_old = current_year - year_int
                    if 1 <= years_old <= 10:
                        # Sweet spot: 1-10 years old get full bonus
                        score += 0.05
                    elif years_old < 1:
                        # Too new (might not have enough reviews)
                        score += 0.02
                    elif years_old <= 20:
                        # Older but still relevant
                        score += 0.03
                    # Classics (>20 years) get no bonus but no penalty
                except (ValueError, TypeError):
                    pass

            # Embedding similarity (using pre-computed embeddings)
            if i in embedding_map and self._user_profile_embedding:
                try:
                    emb = embedding_map[i]
                    sim = self.embedding_service.cosine_similarity(self._user_profile_embedding, emb)
                    if sim > 0.3:
                        score += (sim - 0.3) * 0.12

                    # Penalty for similarity to dismissed content
                    if self._dismissed_embeddings:
                        max_dismissed = max(
                            self.embedding_service.cosine_similarity(emb, de)
                            for de in self._dismissed_embeddings
                        )
                        if max_dismissed > 0.75:
                            score -= 0.25  # Strong penalty
                        elif max_dismissed > 0.6:
                            score -= 0.15
                        elif max_dismissed > 0.5:
                            score -= 0.08
                except Exception:
                    pass

            candidate["score"] = min(max(score, 0.05), 0.98)

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Deduplicate by title
        seen = set()
        unique = []
        for c in candidates:
            title = c.get("title", "").lower().strip()
            if title and title not in seen:
                seen.add(title)
                unique.append(c)

        return unique

    def _create_recommendation(
        self,
        user: User,
        media_type: MediaType,
        candidate: dict,
    ) -> Recommendation:
        """Create a Recommendation object from a candidate dict."""
        return Recommendation(
            user_id=user.id,
            media_type=media_type,
            external_id=str(candidate["id"]),
            title=candidate.get("title", "Unknown"),
            year=int(candidate["year"]) if candidate.get("year") else None,
            cover_url=candidate.get("poster_url") or candidate.get("cover_url"),
            description=candidate.get("overview"),
            score=candidate.get("score", 0.5),
            source=candidate.get("source", "genre_discover"),
            genre_name=candidate.get("genre_name"),
            tmdb_rating=candidate.get("vote_average"),
            is_streamable=candidate.get("is_streamable", False),
            streaming_providers=candidate.get("streaming_providers"),
            generated_at=datetime.utcnow(),
        )

    def _get_primary_genre(self, genre_ids: list[int], tmdb_type: str) -> str | None:
        """Get primary genre name from TMDB genre IDs."""
        if not genre_ids:
            return None

        reverse_map = {
            "movie": {
                28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
                80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
                14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
                9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
                53: "Thriller", 10752: "War", 37: "Western",
            },
            "tv": {
                10759: "Action & Adventure", 16: "Animation", 35: "Comedy",
                80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
                10762: "Kids", 9648: "Mystery", 10763: "News", 10764: "Reality",
                10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk",
                10768: "War & Politics", 37: "Western",
            },
        }

        mapping = reverse_map.get(tmdb_type, {})
        for gid in genre_ids:
            if gid in mapping:
                return mapping[gid]
        return None

    def _get_tmdb_genre_id(self, genre_name: str, tmdb_type: str) -> int | None:
        """Map genre name to TMDB genre ID."""
        movie_genres = {
            "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35,
            "Crime": 80, "Documentary": 99, "Drama": 18, "Family": 10751,
            "Fantasy": 14, "History": 36, "Horror": 27, "Music": 10402,
            "Mystery": 9648, "Romance": 10749, "Science Fiction": 878,
            "Thriller": 53, "War": 10752, "Western": 37,
        }
        tv_genres = {
            "Action & Adventure": 10759, "Animation": 16, "Comedy": 35,
            "Crime": 80, "Documentary": 99, "Drama": 18, "Family": 10751,
            "Kids": 10762, "Mystery": 9648, "News": 10763, "Reality": 10764,
            "Sci-Fi & Fantasy": 10765, "Soap": 10766, "Talk": 10767,
            "War & Politics": 10768, "Western": 37,
        }
        genres = movie_genres if tmdb_type == "movie" else tv_genres
        return genres.get(genre_name)

    async def _check_streaming_availability(
        self,
        tmdb_id: int,
        tmdb_type: str,
    ) -> tuple[bool, list[str] | None]:
        """Check if content is available on streaming platforms.

        Returns:
            Tuple of (is_streamable, list_of_provider_names)
        """
        cache_key = f"{tmdb_type}_{tmdb_id}"
        cached = self._streaming_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            providers = await tmdb_service.get_watch_providers(
                tmdb_id, tmdb_type, self.STREAMING_COUNTRY
            )
            if providers and providers.get("flatrate"):
                # Has subscription streaming
                provider_names = [p.get("provider_name", "") for p in providers["flatrate"]]
                result = (True, provider_names)
            else:
                result = (False, None)

            self._streaming_cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.debug(f"Failed to check streaming for {tmdb_type}/{tmdb_id}: {e}")
            return (False, None)

    async def _enrich_with_streaming(
        self,
        candidates: list[dict],
        tmdb_type: str,
    ) -> list[dict]:
        """Add streaming availability info to candidates."""
        for candidate in candidates:
            tmdb_id = candidate.get("id")
            if tmdb_id:
                is_streamable, providers = await self._check_streaming_availability(tmdb_id, tmdb_type)
                candidate["is_streamable"] = is_streamable
                candidate["streaming_providers"] = providers

                # Boost score for streamable content
                if is_streamable and "score" in candidate:
                    candidate["score"] = min(candidate["score"] + 0.05, 0.98)

        return candidates

    async def _get_user_media(self, user_id: int, media_type: MediaType) -> list[Media]:
        """Get user's media with relationships."""
        result = await self.db.execute(
            select(Media)
            .options(selectinload(Media.genres), selectinload(Media.youtube_metadata))
            .where(and_(Media.user_id == user_id, Media.type == media_type))
        )
        return list(result.scalars().all())

    async def _get_existing_recommendations(self, user_id: int) -> dict[MediaType, list[Recommendation]]:
        """Get existing recommendations grouped by type."""
        result = await self.db.execute(
            select(Recommendation)
            .where(and_(Recommendation.user_id == user_id, Recommendation.is_dismissed == False))
            .order_by(Recommendation.score.desc())
        )
        recommendations = result.scalars().all()

        grouped: dict[MediaType, list[Recommendation]] = {
            MediaType.FILM: [], MediaType.SERIES: [], MediaType.BOOK: [], MediaType.YOUTUBE: [],
        }
        for rec in recommendations:
            if rec.media_type in grouped:
                grouped[rec.media_type].append(rec)
        return grouped

    async def dismiss_recommendation(self, user_id: int, recommendation_id: int) -> bool:
        """Dismiss a recommendation."""
        result = await self.db.execute(
            select(Recommendation).where(
                and_(Recommendation.id == recommendation_id, Recommendation.user_id == user_id)
            )
        )
        rec = result.scalar_one_or_none()
        if rec:
            rec.is_dismissed = True
            await self.db.commit()
            return True
        return False

    async def mark_added_to_library(self, user_id: int, external_id: str, media_type: MediaType) -> bool:
        """Mark recommendation as added to library."""
        result = await self.db.execute(
            select(Recommendation).where(
                and_(
                    Recommendation.user_id == user_id,
                    Recommendation.external_id == external_id,
                    Recommendation.media_type == media_type,
                )
            )
        )
        rec = result.scalar_one_or_none()
        if rec:
            rec.added_to_library = True
            await self.db.commit()
            return True
        return False
