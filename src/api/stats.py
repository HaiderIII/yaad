"""Statistics API endpoints."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Integer, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.models.media import Genre, Media, MediaStatus, media_genres
from src.models.user import User

router = APIRouter()


class TypeCount(BaseModel):
    """Count per media type."""

    type: str
    label: str
    count: int
    color: str


class StatusCount(BaseModel):
    """Count per status."""

    status: str
    label: str
    count: int


class GenreCount(BaseModel):
    """Count per genre."""

    name: str
    count: int


class MonthlyActivity(BaseModel):
    """Monthly activity data."""

    month: str  # YYYY-MM format
    added: int
    finished: int


class YearCount(BaseModel):
    """Count per release year."""

    year: int
    count: int


class RatingDistribution(BaseModel):
    """Rating distribution bucket."""

    rating: float
    count: int


class StatsResponse(BaseModel):
    """Full statistics response."""

    # Summary
    total_media: int
    total_finished: int
    total_in_progress: int
    total_to_consume: int
    average_rating: float | None
    total_watch_time_minutes: int
    total_pages_read: int

    # Breakdowns
    by_type: list[TypeCount]
    by_status: list[StatusCount]
    top_genres: list[GenreCount]
    monthly_activity: list[MonthlyActivity]
    by_release_year: list[YearCount]
    rating_distribution: list[RatingDistribution]


TYPE_CONFIG = {
    "film": {"label": "Films", "color": "#3b82f6"},  # blue
    "series": {"label": "Series", "color": "#06b6d4"},  # cyan
    "book": {"label": "Books", "color": "#22c55e"},  # green
    "youtube": {"label": "Videos", "color": "#ef4444"},  # red
    "podcast": {"label": "Podcasts", "color": "#a855f7"},  # purple
    "show": {"label": "Shows", "color": "#ec4899"},  # pink
}

STATUS_LABELS = {
    "to_consume": "To Consume",
    "in_progress": "In Progress",
    "finished": "Finished",
    "abandoned": "Abandoned",
}


@router.get("", response_model=StatsResponse)
async def get_stats(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    """Get detailed statistics for the current user."""
    user_filter = Media.user_id == user.id
    twelve_months_ago = datetime.now(UTC) - timedelta(days=365)

    # Build all queries
    summary_query = select(
        func.count(Media.id).label("total"),
        func.sum(func.cast(Media.status == MediaStatus.FINISHED, Integer)).label("finished"),
        func.sum(func.cast(Media.status == MediaStatus.IN_PROGRESS, Integer)).label("in_progress"),
        func.sum(func.cast(Media.status == MediaStatus.TO_CONSUME, Integer)).label("to_consume"),
        func.avg(Media.rating).label("avg_rating"),
        func.coalesce(func.sum(Media.duration_minutes), 0).label("total_minutes"),
        func.coalesce(func.sum(Media.page_count), 0).label("total_pages"),
    ).where(user_filter)

    type_query = (
        select(Media.type, func.count(Media.id).label("count"))
        .where(user_filter)
        .group_by(Media.type)
        .order_by(func.count(Media.id).desc())
    )

    status_query = (
        select(Media.status, func.count(Media.id).label("count"))
        .where(user_filter)
        .group_by(Media.status)
        .order_by(func.count(Media.id).desc())
    )

    genre_query = (
        select(Genre.name, func.count(Media.id).label("count"))
        .select_from(Media)
        .join(media_genres, Media.id == media_genres.c.media_id)
        .join(Genre, media_genres.c.genre_id == Genre.id)
        .where(user_filter)
        .group_by(Genre.name)
        .order_by(func.count(Media.id).desc())
        .limit(10)
    )

    added_query = (
        select(
            func.to_char(Media.created_at, "YYYY-MM").label("month"),
            func.count(Media.id).label("count"),
        )
        .where(user_filter, Media.created_at >= twelve_months_ago)
        .group_by(text("month"))
        .order_by(text("month"))
    )

    finished_query = (
        select(
            func.to_char(Media.consumed_at, "YYYY-MM").label("month"),
            func.count(Media.id).label("count"),
        )
        .where(user_filter, Media.consumed_at.is_not(None), Media.consumed_at >= twelve_months_ago)
        .group_by(text("month"))
        .order_by(text("month"))
    )

    year_query = (
        select(Media.year, func.count(Media.id).label("count"))
        .where(user_filter, Media.year.is_not(None))
        .group_by(Media.year)
        .order_by(func.count(Media.id).desc())
        .limit(15)
    )

    rating_query = (
        select(func.floor(Media.rating).label("rating_floor"), func.count(Media.id).label("count"))
        .where(user_filter, Media.rating.is_not(None))
        .group_by(text("rating_floor"))
        .order_by(text("rating_floor"))
    )

    # Execute all queries in parallel
    (
        summary_result,
        type_result,
        status_result,
        genre_result,
        added_result,
        finished_result,
        year_result,
        rating_result,
    ) = await asyncio.gather(
        db.execute(summary_query),
        db.execute(type_query),
        db.execute(status_query),
        db.execute(genre_query),
        db.execute(added_query),
        db.execute(finished_query),
        db.execute(year_query),
        db.execute(rating_query),
    )

    # Process results
    summary = summary_result.one()

    by_type = [
        TypeCount(
            type=row.type.value,
            label=TYPE_CONFIG.get(row.type.value, {}).get("label", row.type.value),
            count=row.count,
            color=TYPE_CONFIG.get(row.type.value, {}).get("color", "#6b7280"),
        )
        for row in type_result.all()
    ]

    by_status = [
        StatusCount(
            status=row.status.value,
            label=STATUS_LABELS.get(row.status.value, row.status.value),
            count=row.count,
        )
        for row in status_result.all()
    ]

    top_genres = [GenreCount(name=row.name, count=row.count) for row in genre_result.all()]

    added_by_month = {row.month: row.count for row in added_result.all()}
    finished_by_month = {row.month: row.count for row in finished_result.all()}
    all_months = sorted(set(added_by_month.keys()) | set(finished_by_month.keys()))
    monthly_activity = [
        MonthlyActivity(
            month=month,
            added=added_by_month.get(month, 0),
            finished=finished_by_month.get(month, 0),
        )
        for month in all_months
    ]

    by_release_year = sorted(
        [YearCount(year=row.year, count=row.count) for row in year_result.all()],
        key=lambda x: x.year,
    )

    rating_distribution = [
        RatingDistribution(rating=float(row.rating_floor), count=row.count)
        for row in rating_result.all()
    ]

    return StatsResponse(
        total_media=summary.total or 0,
        total_finished=summary.finished or 0,
        total_in_progress=summary.in_progress or 0,
        total_to_consume=summary.to_consume or 0,
        average_rating=round(summary.avg_rating, 2) if summary.avg_rating else None,
        total_watch_time_minutes=summary.total_minutes or 0,
        total_pages_read=summary.total_pages or 0,
        by_type=by_type,
        by_status=by_status,
        top_genres=top_genres,
        monthly_activity=monthly_activity,
        by_release_year=by_release_year,
        rating_distribution=rating_distribution,
    )
