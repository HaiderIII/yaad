"""Web routes for Jinja2 templates."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user, get_optional_user
from src.db import get_db
from src.db.crud import (
    get_incomplete_count,
    get_incomplete_media,
    get_media,
    get_media_list,
    get_recent_media,
    get_unfinished_media,
    get_user_stats,
)
from src.models.media import MediaStatus, MediaType
from src.models.user import User
from src.web.context import get_base_context

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
    stats = await get_user_stats(db, user.id)
    recent = await get_recent_media(db, user.id, limit=6)
    unfinished = await get_unfinished_media(db, user.id, limit=20)

    context = get_base_context(request, user)
    context["stats"] = stats
    context["recent_media"] = recent
    context["unfinished_media"] = unfinished
    return templates.TemplateResponse("pages/dashboard.html", context)


@web_router.get("/catalogue", response_class=HTMLResponse)
async def catalogue_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    incomplete: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render catalogue page."""
    page_size = 24
    media_type = MediaType(type) if type else None
    media_status = MediaStatus(status) if status else None
    show_incomplete = incomplete == "1"

    # Get incomplete count for the tab badge
    incomplete_count = await get_incomplete_count(db, user.id)

    if show_incomplete:
        # Show only incomplete media
        items, total = await get_incomplete_media(
            db=db,
            user_id=user.id,
            media_type=media_type,
            page=page,
            page_size=page_size,
        )
    else:
        # Normal media list with filters
        items, total = await get_media_list(
            db=db,
            user_id=user.id,
            media_type=media_type,
            status=media_status,
            search=search,
            page=page,
            page_size=page_size,
        )

    pages = (total + page_size - 1) // page_size if total > 0 else 0

    context = get_base_context(request, user)
    context["media_list"] = items
    context["total"] = total
    context["page"] = page
    context["pages"] = pages
    context["current_type"] = type
    context["current_status"] = status
    context["search"] = search or ""
    context["show_incomplete"] = show_incomplete
    context["incomplete_count"] = incomplete_count
    return templates.TemplateResponse("pages/catalogue.html", context)


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
    import asyncio
    import logging
    from datetime import datetime, timedelta, timezone

    from src.services.metadata.justwatch import justwatch_service
    from src.services.metadata.tmdb import tmdb_service

    logger = logging.getLogger(__name__)

    media = await get_media(db, media_id, user.id)
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
            now = datetime.now(timezone.utc)

            # Check if we need to refresh deep links cache (older than 7 days)
            should_refresh_links = (
                media.streaming_links is None
                or media.streaming_links_updated is None
                or now - media.streaming_links_updated > timedelta(days=7)
            )

            # Always start with existing cache if available
            deep_links = media.streaming_links or {}

            # Prepare async tasks - run API calls in parallel
            async def fetch_justwatch():
                if not should_refresh_links:
                    return None
                try:
                    return await justwatch_service.get_streaming_links(
                        tmdb_id,
                        media_type=media_type,
                        country=user.country,
                        title=media.title,
                        year=media.year,
                    )
                except Exception as e:
                    logger.warning(f"JustWatch API failed for {media.title}: {e}")
                    return None

            async def fetch_watch_providers():
                try:
                    return await tmdb_service.get_watch_providers(
                        tmdb_id, media_type=media_type, country=user.country
                    )
                except Exception as e:
                    logger.warning(f"TMDB watch providers failed for {media.title}: {e}")
                    return None

            async def fetch_all_providers():
                try:
                    return await tmdb_service.get_available_providers(user.country)
                except Exception as e:
                    logger.warning(f"TMDB available providers failed for {user.country}: {e}")
                    return []

            # Run all API calls in parallel
            jw_result, watch_providers, all_providers = await asyncio.gather(
                fetch_justwatch(),
                fetch_watch_providers(),
                fetch_all_providers(),
            )

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
            other_streaming = []
            rent_providers = []
            buy_providers = []

            for provider_id_str, link_info in deep_links.items():
                try:
                    provider_id = int(provider_id_str)
                except (ValueError, TypeError):
                    continue

                provider_name = link_info.get("provider_name", f"Provider {provider_id}")

                # Skip "with Ads" variants
                if "with Ads" in provider_name or "With Ads" in provider_name:
                    continue

                link_type = link_info.get("type", "")
                logo = tmdb_logos.get(provider_id) or tmdb_logos_by_name.get(provider_name.lower())

                provider = {
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "logo_path": logo,
                    "deep_link": link_info.get("url"),
                }

                if link_type == "flatrate":
                    if provider_id in user_platforms:
                        available_on_user_platforms.append(provider)
                    else:
                        other_streaming.append(provider)
                elif link_type == "rent":
                    rent_providers.append(provider)
                elif link_type == "buy":
                    buy_providers.append(provider)

            # Add TMDB flatrate providers that JustWatch might have missed
            if watch_providers:
                seen_ids = {p["provider_id"] for p in available_on_user_platforms + other_streaming}
                for p in watch_providers.get("flatrate", []):
                    if p["provider_id"] not in seen_ids:
                        provider = {
                            "provider_id": p["provider_id"],
                            "provider_name": p["provider_name"],
                            "logo_path": p.get("logo_path"),
                            "deep_link": None,
                        }
                        if p["provider_id"] in user_platforms:
                            available_on_user_platforms.append(provider)
                        else:
                            other_streaming.append(provider)

            if available_on_user_platforms or other_streaming or rent_providers or buy_providers:
                streaming_info = {
                    "available_on_user_platforms": available_on_user_platforms,
                    "other_streaming": other_streaming,
                    "rent": rent_providers,
                    "buy": buy_providers,
                    "link": watch_providers.get("link") if watch_providers else None,
                }
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid external_id for media {media_id}: {e}")

    context["streaming_info"] = streaming_info
    context["user_country"] = user.country

    return templates.TemplateResponse("pages/detail/media.html", context)


@web_router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    """Render settings page."""
    from src.services.metadata.tmdb import tmdb_service

    # Get available streaming providers for user's country
    providers = await tmdb_service.get_available_providers(user.country)

    context = get_base_context(request, user)
    context["available_providers"] = providers[:30]  # Top 30 providers
    context["user_providers"] = user.streaming_platforms or []
    context["user_country"] = user.country
    return templates.TemplateResponse("pages/settings/index.html", context)
