"""Web routes for Jinja2 templates."""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user, get_optional_user
from src.constants import (
    CATALOGUE_PAGE_SIZE,
    MAX_RECENT_ITEMS,
    MAX_UNFINISHED_ITEMS,
    STREAMING_LINKS_REFRESH_DAYS,
)
from src.db import get_db
from src.db.crud import (
    get_genres_for_type,
    get_incomplete_count,
    get_incomplete_media,
    get_media,
    get_media_list,
    get_recent_media,
    get_unfinished_media,
    get_unrated_count,
    get_user_stats,
)
from src.models.media import MediaStatus, MediaType
from src.models.recommendation import Recommendation
from src.models.user import User
from src.services.metadata.justwatch import justwatch_service
from src.services.metadata.tmdb import tmdb_service
from src.web.context import get_base_context

logger = logging.getLogger(__name__)

web_router = APIRouter()
templates = Jinja2Templates(directory="templates")


def format_duration(minutes: int | None) -> str:
    """Format duration in a human-readable way.

    - Under 60 min: show as "45 min"
    - 60+ min: show as "1h30" or "2h"
    """
    if not minutes:
        return ""
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        return f"{hours}h"
    return f"{hours}h{remaining:02d}"


# Register custom filters
templates.env.filters["format_duration"] = format_duration


@web_router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request,
    user: Annotated[User | None, Depends(get_optional_user)],
) -> HTMLResponse | RedirectResponse:
    """Render login page."""
    if user:
        return RedirectResponse(url="/", status_code=302)

    # Clear any stale OAuth state from previous attempts
    # This prevents the "double-click" issue where old oauth_state
    # causes state mismatch on callback
    if "oauth_state" in request.session:
        del request.session["oauth_state"]
    if "oauth_provider" in request.session:
        del request.session["oauth_provider"]

    context = get_base_context(request)
    return templates.TemplateResponse("auth/login.html", context)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Render dashboard page."""
    t0 = time.perf_counter()
    user_platforms = set(str(p) for p in (user.streaming_platforms or []))

    async def get_recommendations():
        """Fetch top recommendations for homepage (limit 10, mixed types)."""
        result = await db.execute(
            select(Recommendation)
            .where(Recommendation.user_id == user.id)
            .where(Recommendation.is_dismissed == False)  # noqa: E712
            .where(Recommendation.added_to_library == False)  # noqa: E712
            .order_by(Recommendation.score.desc())
            .limit(10)
        )
        return result.scalars().all()

    # Run all queries in parallel for better performance
    stats, recent, unfinished, recommendations = await asyncio.gather(
        get_user_stats(db, user.id),
        get_recent_media(db, user.id, limit=MAX_RECENT_ITEMS),
        get_unfinished_media(db, user.id, limit=MAX_UNFINISHED_ITEMS, user_platforms=user_platforms),
        get_recommendations(),
    )
    t1 = time.perf_counter()
    logger.info(f"[PERF] dashboard DB queries took {t1 - t0:.3f}s")

    context = get_base_context(request, user)
    context["stats"] = stats
    context["recent_media"] = recent
    context["unfinished_media"] = unfinished
    context["recommendations"] = recommendations
    context["user_platforms"] = user_platforms

    t2 = time.perf_counter()
    response = templates.TemplateResponse("pages/dashboard.html", context)
    t3 = time.perf_counter()
    logger.info(f"[PERF] dashboard template render took {t3 - t2:.3f}s, total={t3 - t0:.3f}s")
    return response


@web_router.get("/catalogue", response_class=HTMLResponse)
async def catalogue_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    genre: Annotated[str | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_order: Annotated[str, Query()] = "desc",
    incomplete: Annotated[str | None, Query()] = None,
    streamable: Annotated[str | None, Query()] = None,
    unrated: Annotated[str | None, Query()] = None,
    partial: Annotated[str | None, Query()] = None,
    grid_only: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render catalogue page.

    If partial=1, return the full catalogue content partial (tabs + filters + grid).
    If grid_only=1, return only the media grid partial.
    """
    page_size = CATALOGUE_PAGE_SIZE
    media_type = MediaType(type) if type else None
    media_status = MediaStatus(status) if status else None
    show_incomplete = incomplete == "1"
    streamable_only = streamable == "1"
    unrated_only = unrated == "1"
    is_partial = partial == "1"
    is_grid_only = grid_only == "1"
    # Convert to set of strings for Jinja template comparisons
    user_platforms_str = set(str(p) for p in (user.streaming_platforms or []))
    user_platforms = set(user.streaming_platforms or [])

    # Validate sort parameters
    valid_sort_by = ["created_at", "title", "year", "rating", "updated_at"]
    if sort_by not in valid_sort_by:
        sort_by = "created_at"
    if sort_order not in ["asc", "desc"]:
        sort_order = "desc"

    # Parallel fetch: incomplete count + unrated count + genres + media list
    # This reduces sequential DB round-trips to 1 parallel batch
    async def fetch_media():
        if show_incomplete:
            return await get_incomplete_media(
                db=db,
                user_id=user.id,
                media_type=media_type,
                page=page,
                page_size=page_size,
            )
        else:
            return await get_media_list(
                db=db,
                user_id=user.id,
                media_type=media_type,
                status=media_status,
                search=search,
                genre=genre,
                sort_by=sort_by,
                sort_order=sort_order,
                page=page,
                page_size=page_size,
                streamable_only=streamable_only,
                user_platforms=user_platforms if streamable_only else None,
                unrated_only=unrated_only,
            )

    t0 = time.perf_counter()
    incomplete_count, unrated_count, genres, (items, total) = await asyncio.gather(
        get_incomplete_count(db, user.id),
        get_unrated_count(db, user.id),
        get_genres_for_type(db, user.id, media_type),
        fetch_media(),
    )
    t1 = time.perf_counter()
    logger.info(f"[PERF] catalogue DB queries took {t1 - t0:.3f}s")

    pages = (total + page_size - 1) // page_size if total > 0 else 0

    t2 = time.perf_counter()
    context = get_base_context(request, user)
    context["media_list"] = items
    context["total"] = total
    context["page"] = page
    context["pages"] = pages
    context["current_type"] = type
    context["current_status"] = status
    context["current_genre"] = genre
    context["current_sort_by"] = sort_by
    context["current_sort_order"] = sort_order
    context["search"] = search or ""
    context["show_incomplete"] = show_incomplete
    context["streamable_only"] = streamable_only
    context["unrated_only"] = unrated_only
    context["incomplete_count"] = incomplete_count
    context["unrated_count"] = unrated_count
    context["genres"] = genres
    # Pass user's streaming platforms for availability indicator
    context["user_platforms"] = user_platforms_str
    t3 = time.perf_counter()
    logger.info(f"[PERF] catalogue context build took {t3 - t2:.3f}s")

    # Return the catalogue content partial for HTMX tab switching
    if is_partial:
        t4 = time.perf_counter()
        response = templates.TemplateResponse("partials/catalogue_content.html", context)
        t5 = time.perf_counter()
        logger.info(f"[PERF] catalogue partial template render took {t5 - t4:.3f}s")
        return response

    # Return only the grid partial for HTMX filter updates
    if is_grid_only:
        t4 = time.perf_counter()
        response = templates.TemplateResponse("partials/media_grid.html", context)
        t5 = time.perf_counter()
        logger.info(f"[PERF] catalogue grid template render took {t5 - t4:.3f}s")
        return response

    t4 = time.perf_counter()
    response = templates.TemplateResponse("pages/catalogue.html", context)
    t5 = time.perf_counter()
    logger.info(f"[PERF] catalogue full template render took {t5 - t4:.3f}s, total={t5 - t0:.3f}s")
    return response


@web_router.get("/add", response_class=HTMLResponse)
async def add_media_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    """Render add media page."""
    context = get_base_context(request, user)
    return templates.TemplateResponse("pages/add.html", context)


@web_router.get("/media/{media_id}", response_class=HTMLResponse, response_model=None)
async def media_detail_page(
    request: Request,
    media_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse | RedirectResponse:
    """Render media detail page."""
    t0 = time.perf_counter()

    media = await get_media(db, media_id, user.id)
    t1 = time.perf_counter()
    logger.info(f"[PERF] media detail DB query took {t1 - t0:.3f}s")
    if not media:
        return RedirectResponse(url="/catalogue", status_code=302)

    context = get_base_context(request, user)
    context["media"] = media

    # Get streaming availability for films and series
    streaming_info = None

    if media.type.value in ["film", "series"] and media.external_id:
        try:
            tmdb_id = int(media.external_id)
            media_type = "movie" if media.type.value == "film" else "tv"
            now = datetime.now(UTC)

            # Check if we need to refresh deep links cache (older than 7 days)
            # Also refresh if cache contains old non-normalized IDs (e.g., "9" instead of "119" for Prime)
            has_old_ids = media.streaming_links and "9" in media.streaming_links
            should_refresh_links = (
                media.streaming_links is None
                or media.streaming_links_updated is None
                or now - media.streaming_links_updated > timedelta(days=STREAMING_LINKS_REFRESH_DAYS)
                or has_old_ids
            )

            # Always start with existing cache if available
            deep_links = media.streaming_links or {}

            # API timeout to prevent page hangs (5 seconds per service)
            API_TIMEOUT = 5.0

            # Prepare async tasks - run API calls in parallel with timeout protection
            async def fetch_justwatch():
                if not should_refresh_links:
                    return None
                try:
                    return await asyncio.wait_for(
                        justwatch_service.get_streaming_links(
                            tmdb_id,
                            media_type=media_type,
                            country=user.country,
                            title=media.title,
                            year=media.year,
                        ),
                        timeout=API_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"JustWatch API timeout for {media.title}")
                    return None
                except Exception as e:
                    logger.warning(f"JustWatch API failed for {media.title}: {e}")
                    return None

            async def fetch_watch_providers():
                try:
                    return await asyncio.wait_for(
                        tmdb_service.get_watch_providers(
                            tmdb_id, media_type=media_type, country=user.country
                        ),
                        timeout=API_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"TMDB watch providers timeout for {media.title}")
                    return None
                except Exception as e:
                    logger.warning(f"TMDB watch providers failed for {media.title}: {e}")
                    return None

            async def fetch_all_providers():
                try:
                    return await asyncio.wait_for(
                        tmdb_service.get_available_providers(user.country),
                        timeout=API_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"TMDB available providers timeout for {user.country}")
                    return []
                except Exception as e:
                    logger.warning(f"TMDB available providers failed for {user.country}: {e}")
                    return []

            # Run all API calls in parallel
            t_api_start = time.perf_counter()
            jw_result, watch_providers, all_providers = await asyncio.gather(
                fetch_justwatch(),
                fetch_watch_providers(),
                fetch_all_providers(),
            )
            t_api_end = time.perf_counter()
            logger.info(f"[PERF] media detail API calls took {t_api_end - t_api_start:.3f}s")

            # Update cache if JustWatch returned new data
            if jw_result and jw_result.get("links"):
                media.streaming_links = jw_result["links"]
                media.streaming_links_updated = now
                await db.commit()
                deep_links = jw_result["links"]

            # Build logo lookup from TMDB data (by ID and by name)
            tmdb_logos: dict[int, str] = {}
            tmdb_logos_by_name: dict[str, str] = {}

            if watch_providers:
                for category in ["flatrate", "rent", "buy"]:
                    for p in watch_providers.get(category, []):
                        if p.get("logo_path"):
                            tmdb_logos[p["provider_id"]] = p["logo_path"]
                            tmdb_logos_by_name[p["provider_name"].lower()] = p["logo_path"]

            for p in all_providers:
                if p.get("logo_path"):
                    if p["provider_id"] not in tmdb_logos:
                        tmdb_logos[p["provider_id"]] = p["logo_path"]
                    name_lower = p["provider_name"].lower()
                    if name_lower not in tmdb_logos_by_name:
                        tmdb_logos_by_name[name_lower] = p["logo_path"]

            # Process providers from JustWatch
            user_platforms = set(user.streaming_platforms or [])
            available_on_user_platforms = []
            rent_providers = []
            buy_providers = []

            for provider_id_str, link_info in deep_links.items():
                try:
                    provider_id = int(provider_id_str)
                except (ValueError, TypeError):
                    continue

                provider_name = link_info.get("provider_name", f"Provider {provider_id}")

                # Skip "with Ads" variants and Amazon Channels (paid add-ons)
                if "with Ads" in provider_name or "With Ads" in provider_name:
                    continue
                if "Amazon Channel" in provider_name or "amazon channel" in provider_name.lower():
                    continue

                link_type = link_info.get("type", "")
                logo = tmdb_logos.get(provider_id) or tmdb_logos_by_name.get(provider_name.lower())

                provider = {
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "logo_path": logo,
                    "deep_link": link_info.get("url"),
                }

                # Show flatrate/free platforms only if user has selected them
                # free = free without ads (Arte, France TV, etc.)
                # Skip "ads" type (free with ads like Pluto TV, Tubi, etc.)
                if link_type in ("flatrate", "free"):
                    if provider_id in user_platforms:
                        available_on_user_platforms.append(provider)
                elif link_type == "rent":
                    rent_providers.append(provider)
                elif link_type == "buy":
                    buy_providers.append(provider)

            # Add TMDB flatrate providers that JustWatch might have missed
            if watch_providers:
                seen_ids = {p["provider_id"] for p in available_on_user_platforms}
                for p in watch_providers.get("flatrate", []):
                    pid = p["provider_id"]
                    if pid not in seen_ids and pid in user_platforms:
                        provider = {
                            "provider_id": pid,
                            "provider_name": p["provider_name"],
                            "logo_path": p.get("logo_path"),
                            "deep_link": None,
                        }
                        available_on_user_platforms.append(provider)

            if available_on_user_platforms or rent_providers or buy_providers:
                streaming_info = {
                    "available_on_user_platforms": available_on_user_platforms,
                    "other_streaming": [],  # No longer showing platforms user hasn't selected
                    "rent": rent_providers,
                    "buy": buy_providers,
                    "link": watch_providers.get("link") if watch_providers else None,
                }
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid external_id for media {media_id}: {e}")

    context["streaming_info"] = streaming_info
    context["user_country"] = user.country

    # Letterboxd friends ratings support
    context["letterboxd_connected"] = bool(user.letterboxd_username)
    # Use stored Letterboxd slug if available (from imports), otherwise generate from title
    if media.type.value == "film" and user.letterboxd_username:
        if media.letterboxd_slug:
            # Use the slug stored during import (more reliable)
            context["film_slug"] = media.letterboxd_slug
        else:
            # Fallback: generate from title for manually added films
            # e.g. "One Flew Over the Cuckoo's Nest" -> "one-flew-over-the-cuckoos-nest"
            slug = media.title.lower()
            slug = re.sub(r"[''`]", "", slug)  # Remove apostrophes
            slug = re.sub(r"[^a-z0-9]+", "-", slug)  # Replace non-alphanumeric with hyphens
            slug = slug.strip("-")  # Remove leading/trailing hyphens
            context["film_slug"] = slug
    else:
        context["film_slug"] = ""

    t_render_start = time.perf_counter()
    response = templates.TemplateResponse("pages/detail/media.html", context)
    t_render_end = time.perf_counter()
    logger.info(f"[PERF] media detail template render took {t_render_end - t_render_start:.3f}s, total={t_render_end - t0:.3f}s")
    return response


@web_router.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Render statistics page."""
    # Import here to avoid circular dependency
    from src.api.stats import get_stats

    # Get full stats from API endpoint logic (reuse the same function)
    stats = await get_stats(user=user, db=db)

    context = get_base_context(request, user)
    context["stats"] = stats.model_dump(mode="json")
    return templates.TemplateResponse("pages/stats.html", context)


@web_router.get("/recommendations", response_class=HTMLResponse)
async def recommendations_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """Render recommendations page with genre-based sliders."""
    t0 = time.perf_counter()
    from sqlalchemy import and_, select

    from src.models.recommendation import Recommendation

    # Get recommendations for this user
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
    t1 = time.perf_counter()
    logger.info(f"[PERF] recommendations DB query took {t1 - t0:.3f}s")

    # Group by media type, then by genre within each type
    type_mapping = {
        MediaType.FILM: "films",
        MediaType.SERIES: "series",
        MediaType.BOOK: "books",
        MediaType.YOUTUBE: "youtube",
    }

    # Structure: {type: {genre: [recs]}}
    grouped_by_genre = {
        "films": {},
        "series": {},
        "books": {},
        "youtube": {},
    }

    # Track genre scores for sorting (avg score of recommendations in each genre)
    genre_scores: dict[str, dict[str, list[float]]] = {
        "films": {}, "series": {}, "books": {}, "youtube": {}
    }

    for rec in recommendations:
        type_key = type_mapping.get(rec.media_type)
        if not type_key:
            continue

        genre = rec.genre_name or "Découvertes"

        if genre not in grouped_by_genre[type_key]:
            grouped_by_genre[type_key][genre] = []
            genre_scores[type_key][genre] = []

        # Limit per genre
        if len(grouped_by_genre[type_key][genre]) < 10:
            grouped_by_genre[type_key][genre].append(rec)
            genre_scores[type_key][genre].append(rec.score)

    # Sort genres by average score (highest first) to show user's preferred genres first
    for type_key in grouped_by_genre:
        if grouped_by_genre[type_key]:
            sorted_genres = sorted(
                grouped_by_genre[type_key].items(),
                key=lambda x: sum(genre_scores[type_key].get(x[0], [0])) / max(len(genre_scores[type_key].get(x[0], [1])), 1),
                reverse=True
            )
            grouped_by_genre[type_key] = dict(sorted_genres)

    # Category colors and icons for consistent styling
    category_styles = {
        "films": {"color": "blue", "icon": "film"},
        "series": {"color": "cyan", "icon": "tv"},
        "books": {"color": "emerald", "icon": "book"},
        "youtube": {"color": "red", "icon": "video"},
    }

    context = get_base_context(request, user)
    context["recommendations_by_genre"] = grouped_by_genre
    context["category_styles"] = category_styles
    context["user_platforms"] = set(str(p) for p in (user.streaming_platforms or []))

    t2 = time.perf_counter()
    response = templates.TemplateResponse("pages/recommendations.html", context)
    t3 = time.perf_counter()
    logger.info(f"[PERF] recommendations template render took {t3 - t2:.3f}s, total={t3 - t0:.3f}s")
    return response


@web_router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    """Render settings page."""
    # Get available streaming providers for user's country
    providers = await tmdb_service.get_available_providers(user.country)

    context = get_base_context(request, user)
    context["available_providers"] = providers[:30]  # Top 30 providers
    context["user_providers"] = user.streaming_platforms or []
    context["user_country"] = user.country
    return templates.TemplateResponse("pages/settings/index.html", context)


@web_router.get("/offline", response_class=HTMLResponse)
async def offline_page(request: Request) -> HTMLResponse:
    """Render offline fallback page for PWA."""
    return templates.TemplateResponse("offline.html", {"request": request})
