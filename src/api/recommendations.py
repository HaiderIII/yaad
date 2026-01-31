"""Recommendations API endpoints."""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from src.auth.dependencies import get_current_user
from src.db import get_db
from src.db.crud.media import create_media
from src.db.database import async_session_maker
from src.models.media import MediaStatus, MediaType
from src.models.recommendation import Recommendation
from src.models.schemas import MediaCreate, MediaStatusEnum, MediaTypeEnum
from src.models.user import User
from src.services.metadata.books import book_service
from src.services.metadata.justwatch import justwatch_service
from src.services.metadata.tmdb import tmdb_service
from src.services.recommendations import RecommendationEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


class RecommendationResponse(BaseModel):
    """Response model for a single recommendation."""

    id: int
    media_type: str
    external_id: str
    title: str
    year: int | None
    cover_url: str | None
    description: str | None
    score: float
    source: str
    external_url: str | None

    class Config:
        from_attributes = True


class RecommendationsByTypeResponse(BaseModel):
    """Response model for recommendations grouped by type."""

    films: list[RecommendationResponse]
    series: list[RecommendationResponse]
    books: list[RecommendationResponse]
    youtube: list[RecommendationResponse]


@router.get("", response_model=RecommendationsByTypeResponse)
async def get_recommendations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecommendationsByTypeResponse:
    """Get all recommendations for the current user, grouped by type."""
    result = await db.execute(
        select(Recommendation)
        .where(
            and_(
                Recommendation.user_id == user.id,
                Recommendation.is_dismissed == False,
                Recommendation.added_to_library == False,
            )
        )
        .order_by(Recommendation.score.desc())
    )
    recommendations = result.scalars().all()

    # Group by type
    grouped = {
        "films": [],
        "series": [],
        "books": [],
        "youtube": [],
    }

    type_mapping = {
        MediaType.FILM: "films",
        MediaType.SERIES: "series",
        MediaType.BOOK: "books",
        MediaType.YOUTUBE: "youtube",
    }

    for rec in recommendations:
        key = type_mapping.get(rec.media_type)
        if key and len(grouped[key]) < 10:  # Limit to 10 per type
            grouped[key].append(
                RecommendationResponse(
                    id=rec.id,
                    media_type=rec.media_type.value,
                    external_id=rec.external_id,
                    title=rec.title,
                    year=rec.year,
                    cover_url=rec.cover_url,
                    description=rec.description,
                    score=rec.score,
                    source=rec.source,
                    external_url=rec.external_url,
                )
            )

    return RecommendationsByTypeResponse(**grouped)


@router.post("/generate")
async def generate_recommendations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually trigger recommendation generation for the current user."""
    engine = RecommendationEngine(db)
    result = await engine.generate_recommendations_for_user(user, force_refresh=True)

    counts = {media_type.value: len(recs) for media_type, recs in result.items()}
    return {"status": "success", "generated": counts}


@router.get("/generate/stream")
async def generate_recommendations_stream(
    request: Request,
    mode: str = "full",
    user: User = Depends(get_current_user),
):
    """Stream recommendation generation progress via SSE.

    Args:
        mode: 'full' for complete regeneration, 'complete' to fill gaps only.
    """

    async def event_generator():
        # Create a new session for the background task
        async with async_session_maker() as db:
            engine = RecommendationEngine(db)

            if mode == "complete":
                gen = engine.complete_recommendations_streaming(user)
            else:
                gen = engine.generate_recommendations_streaming(user)

            async for event in gen:
                # Check if client disconnected
                if await request.is_disconnected():
                    logger.info(f"Client disconnected during recommendation generation for user {user.id}")
                    break

                # Send progress event as SSE
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "progress": event.progress,
                        "status": event.status,
                        "step": event.step,
                        "count": event.count,
                    }),
                }

                # If done or error, send final event
                if event.step in ("done", "error"):
                    yield {
                        "event": "complete",
                        "data": json.dumps({
                            "success": event.step == "done",
                            "total": event.count,
                        }),
                    }

    return EventSourceResponse(event_generator())


@router.post("/{recommendation_id}/dismiss")
async def dismiss_recommendation(
    recommendation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Dismiss a recommendation (won't show again)."""
    engine = RecommendationEngine(db)
    success = await engine.dismiss_recommendation(user.id, recommendation_id)

    if not success:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Return empty HTML to remove the card from DOM
    return HTMLResponse(content="", status_code=200)


@router.post("/{recommendation_id}/add")
async def add_recommendation_to_library(
    recommendation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Add recommendation directly to library in background and remove the card."""
    # Get the recommendation
    result = await db.execute(
        select(Recommendation).where(
            and_(
                Recommendation.id == recommendation_id,
                Recommendation.user_id == user.id,
            )
        )
    )
    rec = result.scalar_one_or_none()

    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    try:
        # Fetch metadata from external service
        metadata = None
        genres = []
        directors = []

        if rec.media_type == MediaType.FILM:
            metadata = await tmdb_service.get_movie_details(int(rec.external_id))
            if metadata:
                genres = metadata.get("genres", [])
                directors = [d["name"] for d in metadata.get("directors", [])]
        elif rec.media_type == MediaType.SERIES:
            metadata = await tmdb_service.get_tv_details(int(rec.external_id))
            if metadata:
                genres = metadata.get("genres", [])
                directors = [d["name"] for d in metadata.get("directors", [])]
        elif rec.media_type == MediaType.BOOK:
            metadata = await book_service.get_book_details(rec.external_id)
            if metadata:
                directors = metadata.get("authors", [])  # Books have authors

        # Create media entry
        media_data = MediaCreate(
            type=MediaTypeEnum(rec.media_type.value),
            title=metadata.get("title", rec.title) if metadata else rec.title,
            local_title=metadata.get("local_title") if metadata else None,
            external_id=rec.external_id,
            year=int(metadata.get("year")) if metadata and metadata.get("year") else rec.year,
            description=metadata.get("description", rec.description) if metadata else rec.description,
            cover_url=metadata.get("cover_url", rec.cover_url) if metadata else rec.cover_url,
            external_url=metadata.get("external_url", rec.external_url) if metadata else rec.external_url,
            status=MediaStatusEnum.TO_CONSUME,
            duration_minutes=metadata.get("duration_minutes") if metadata else None,
            # Extended metadata
            tmdb_rating=metadata.get("tmdb_rating") if metadata else None,
            tmdb_vote_count=metadata.get("tmdb_vote_count") if metadata else None,
            popularity=metadata.get("popularity") if metadata else None,
            original_language=metadata.get("original_language") if metadata else None,
            production_countries=metadata.get("production_countries") if metadata else None,
            cast=metadata.get("cast") if metadata else None,
            keywords=metadata.get("keywords") if metadata else None,
            certification=metadata.get("certification") if metadata else None,
            tagline=metadata.get("tagline") if metadata else None,
            # Series-specific
            number_of_seasons=metadata.get("number_of_seasons") if metadata else None,
            number_of_episodes=metadata.get("number_of_episodes") if metadata else None,
            series_status=metadata.get("series_status") if metadata else None,
            networks=metadata.get("networks") if metadata else None,
        )

        new_media = await create_media(db, user.id, media_data, genres=genres, authors=directors)

        # Fetch streaming links for films/series
        if rec.media_type in (MediaType.FILM, MediaType.SERIES) and new_media:
            try:
                tmdb_type = "movie" if rec.media_type == MediaType.FILM else "tv"
                streaming_links = await justwatch_service.get_streaming_links(
                    int(rec.external_id),
                    media_type=tmdb_type,
                    country=user.country,
                    title=rec.title,
                    year=rec.year,
                )
                if streaming_links and streaming_links.get("links"):
                    new_media.streaming_links = streaming_links["links"]
                    new_media.streaming_links_updated = datetime.now(UTC)
                    logger.info(f"Added streaming links for {rec.title}")
            except Exception as e:
                logger.warning(f"Failed to fetch streaming links for {rec.title}: {e}")

        # Mark recommendation as added
        rec.added_to_library = True
        await db.commit()

        logger.info(f"Added recommendation {rec.id} ({rec.title}) to library for user {user.id}")

        # Return empty HTML to remove the card from DOM
        return HTMLResponse(content="", status_code=200)

    except Exception as e:
        logger.error(f"Failed to add recommendation {recommendation_id}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to add media to library")


class RecommendationDetailResponse(BaseModel):
    """Full details for a recommendation including trailer and ratings."""

    id: int
    media_type: str
    external_id: str
    title: str
    original_title: str | None = None
    year: int | None
    cover_url: str | None
    backdrop_url: str | None = None
    description: str | None
    score: float
    source: str
    genre_name: str | None
    external_url: str | None
    # External ratings
    tmdb_rating: float | None = None
    tmdb_vote_count: int | None = None
    # Trailer
    trailer_key: str | None = None  # YouTube video key
    trailer_site: str | None = None
    # Additional info
    runtime: int | None = None
    genres: list[str] = []
    cast: list[dict] = []
    directors: list[str] = []
    tagline: str | None = None


@router.get("/{recommendation_id}/details", response_model=RecommendationDetailResponse)
async def get_recommendation_details(
    recommendation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecommendationDetailResponse:
    """Get full details for a recommendation including trailer and ratings."""
    # Get the recommendation
    result = await db.execute(
        select(Recommendation).where(
            and_(
                Recommendation.id == recommendation_id,
                Recommendation.user_id == user.id,
            )
        )
    )
    rec = result.scalar_one_or_none()

    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # Build response with basic info from recommendation
    response_data = {
        "id": rec.id,
        "media_type": rec.media_type.value,
        "external_id": rec.external_id,
        "title": rec.title,
        "year": rec.year,
        "cover_url": rec.cover_url,
        "description": rec.description,
        "score": rec.score,
        "source": rec.source,
        "genre_name": rec.genre_name,
        "external_url": rec.external_url,
    }

    # Fetch additional metadata from external service
    try:
        if rec.media_type == MediaType.FILM:
            metadata = await tmdb_service.get_movie_details(int(rec.external_id))
            if metadata:
                response_data.update({
                    "original_title": metadata.get("original_title"),
                    "backdrop_url": metadata.get("backdrop_url"),
                    "tmdb_rating": metadata.get("tmdb_rating"),
                    "tmdb_vote_count": metadata.get("tmdb_vote_count"),
                    "runtime": metadata.get("duration_minutes"),
                    "genres": metadata.get("genres", []),
                    "cast": metadata.get("cast", [])[:10],  # Top 10 cast
                    "directors": [d["name"] for d in metadata.get("directors", [])],
                    "tagline": metadata.get("tagline"),
                })

            # Get trailer
            trailer = await tmdb_service.get_trailer(int(rec.external_id), "movie")
            if trailer:
                response_data["trailer_key"] = trailer.get("key")
                response_data["trailer_site"] = trailer.get("site")

        elif rec.media_type == MediaType.SERIES:
            metadata = await tmdb_service.get_tv_details(int(rec.external_id))
            if metadata:
                response_data.update({
                    "original_title": metadata.get("original_title"),
                    "backdrop_url": metadata.get("backdrop_url"),
                    "tmdb_rating": metadata.get("tmdb_rating"),
                    "tmdb_vote_count": metadata.get("tmdb_vote_count"),
                    "runtime": metadata.get("duration_minutes"),
                    "genres": metadata.get("genres", []),
                    "cast": metadata.get("cast", [])[:10],
                    "directors": [d["name"] for d in metadata.get("directors", [])],
                    "tagline": metadata.get("tagline"),
                })

            # Get trailer
            trailer = await tmdb_service.get_trailer(int(rec.external_id), "tv")
            if trailer:
                response_data["trailer_key"] = trailer.get("key")
                response_data["trailer_site"] = trailer.get("site")

        elif rec.media_type == MediaType.BOOK:
            metadata = await book_service.get_book_details(rec.external_id)
            if metadata:
                response_data.update({
                    "directors": metadata.get("authors", []),  # Authors as "directors"
                    "genres": [],
                })

    except Exception as e:
        logger.warning(f"Failed to fetch additional details for recommendation {recommendation_id}: {e}")

    return RecommendationDetailResponse(**response_data)
