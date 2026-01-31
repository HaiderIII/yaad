"""Main FastAPI application."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.api import api_router
from src.config import get_settings
from src.constants import (
    MAX_CONSECUTIVE_FAILURES,
    SESSION_COOKIE_NAME,
    SESSION_TIMEOUT_DAYS,
    STREAMING_RATE_LIMIT_DELAY,
    SYNC_INTERVAL_KOBO,
    SYNC_INTERVAL_LETTERBOXD,
    SYNC_INTERVAL_RECOMMENDATIONS,
    SYNC_INTERVAL_STREAMING,
    SYNC_INTERVAL_YOUTUBE,
)
from src.db import async_session_maker, init_db
from src.utils.cache import cache
from src.utils.http_client import close_all_clients
from src.utils.logging import get_logger, setup_logging
from src.utils.metrics import MetricsMiddleware, metrics
from src.web import web_router

settings = get_settings()
setup_logging()
logger = get_logger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # Content Security Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; "
            "font-src 'self'; "
            "connect-src 'self' https:; "
            "frame-ancestors 'none';"
        )
        # Allow service worker to control the entire app scope
        if request.url.path == "/static/sw.js":
            response.headers["Service-Worker-Allowed"] = "/"
        # HSTS (only in production)
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


async def periodic_kobo_sync(shutdown_event: asyncio.Event) -> None:
    """Background task that syncs Kobo for all connected users periodically."""
    consecutive_failures = 0
    max_failures = MAX_CONSECUTIVE_FAILURES

    while not shutdown_event.is_set():
        try:
            # Wait with cancellation support
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=SYNC_INTERVAL_KOBO
            )
            break  # Shutdown requested
        except TimeoutError:
            pass  # Normal timeout, continue with sync

        try:
            from src.services.kobo.sync import sync_all_kobo_users

            result = await sync_all_kobo_users()
            logger.info(f"Periodic Kobo sync completed: {result}")
            consecutive_failures = 0  # Reset on success
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic Kobo sync failed ({consecutive_failures}/{max_failures}): {e}")
            if consecutive_failures >= max_failures:
                logger.critical("Kobo sync: Too many consecutive failures, backing off")
                await asyncio.sleep(SYNC_INTERVAL_KOBO)  # Extra delay after repeated failures
                consecutive_failures = 0


async def periodic_letterboxd_sync(shutdown_event: asyncio.Event) -> None:
    """Background task that syncs Letterboxd for all configured users periodically."""
    consecutive_failures = 0
    max_failures = MAX_CONSECUTIVE_FAILURES

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=SYNC_INTERVAL_LETTERBOXD
            )
            break
        except TimeoutError:
            pass

        try:
            from src.services.imports.sync import sync_all_letterboxd_users

            result = await sync_all_letterboxd_users(full_import=False)
            logger.info(f"Periodic Letterboxd sync completed: {result}")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic Letterboxd sync failed ({consecutive_failures}/{max_failures}): {e}")
            if consecutive_failures >= max_failures:
                logger.critical("Letterboxd sync: Too many consecutive failures, backing off")
                await asyncio.sleep(SYNC_INTERVAL_LETTERBOXD)
                consecutive_failures = 0


async def periodic_streaming_links_refresh(shutdown_event: asyncio.Event) -> None:
    """Background task that refreshes streaming links for all films/series daily."""
    from datetime import timedelta

    from sqlalchemy import select

    from src.models.media import Media, MediaType
    from src.models.user import User
    from src.services.metadata.justwatch import justwatch_service

    consecutive_failures = 0
    max_failures = 3

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=SYNC_INTERVAL_STREAMING
            )
            break
        except TimeoutError:
            pass

        try:
            async with async_session_maker() as db:
                # Get media with outdated streaming links (> 1 day old)
                cutoff = datetime.now(UTC) - timedelta(days=1)
                query = select(Media).where(
                    Media.type.in_([MediaType.FILM, MediaType.SERIES]),
                    (
                        (Media.streaming_links_updated.is_(None))
                        | (Media.streaming_links_updated < cutoff)
                    ),
                )
                result = await db.execute(query.limit(100))
                media_list = result.scalars().all()

                if not media_list:
                    logger.info("Streaming links refresh: all links are up to date")
                    consecutive_failures = 0
                    continue

                # Get user countries
                users_result = await db.execute(select(User.id, User.country))
                user_countries = {row.id: row.country for row in users_result.all()}

                updated = 0
                errors = 0
                for media in media_list:
                    # Check for shutdown during long-running refresh
                    if shutdown_event.is_set():
                        logger.info("Streaming refresh interrupted by shutdown")
                        break

                    if not media.external_id:
                        continue

                    country = user_countries.get(media.user_id, "FR")
                    media_type = "movie" if media.type == MediaType.FILM else "tv"

                    try:
                        tmdb_id = int(media.external_id)
                        links = await justwatch_service.get_streaming_links(
                            tmdb_id,
                            media_type=media_type,
                            country=country,
                            title=media.title,
                            year=media.year,
                        )

                        if links and links.get("links"):
                            media.streaming_links = links["links"]
                        else:
                            media.streaming_links = {}
                        media.streaming_links_updated = datetime.now(UTC)
                        updated += 1

                        # Rate limiting
                        await asyncio.sleep(STREAMING_RATE_LIMIT_DELAY)
                    except Exception as e:
                        errors += 1
                        logger.debug(f"Failed to refresh streaming for {media.title}: {e}")

                await db.commit()
                logger.info(
                    f"Periodic streaming links refresh: updated {updated}/{len(media_list)} media "
                    f"({errors} errors)"
                )
                consecutive_failures = 0

        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic streaming links refresh failed ({consecutive_failures}/{max_failures}): {e}")
            if consecutive_failures >= max_failures:
                logger.critical("Streaming refresh: Too many failures, backing off")
                await asyncio.sleep(SYNC_INTERVAL_STREAMING)
                consecutive_failures = 0


async def periodic_youtube_sync(shutdown_event: asyncio.Event) -> None:
    """Background task that syncs YouTube Watch Later for all connected users periodically."""
    consecutive_failures = 0
    max_failures = MAX_CONSECUTIVE_FAILURES

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=SYNC_INTERVAL_YOUTUBE
            )
            break
        except TimeoutError:
            pass

        try:
            from src.services.youtube import sync_all_youtube_users

            result = await sync_all_youtube_users()
            logger.info(f"Periodic YouTube sync completed: {result}")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic YouTube sync failed ({consecutive_failures}/{max_failures}): {e}")
            if consecutive_failures >= max_failures:
                logger.critical("YouTube sync: Too many consecutive failures, backing off")
                await asyncio.sleep(SYNC_INTERVAL_YOUTUBE)
                consecutive_failures = 0


async def periodic_recommendations_generation(shutdown_event: asyncio.Event) -> None:
    """Background task that generates AI recommendations for all users (2x/day)."""
    from sqlalchemy import select

    from src.models.user import User
    from src.services.recommendations import RecommendationEngine

    consecutive_failures = 0
    max_failures = 3

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=SYNC_INTERVAL_RECOMMENDATIONS
            )
            break
        except TimeoutError:
            pass

        try:
            async with async_session_maker() as db:
                # Get all users
                result = await db.execute(select(User))
                users = result.scalars().all()

                generated_count = 0
                for user in users:
                    if shutdown_event.is_set():
                        break
                    try:
                        engine = RecommendationEngine(db)
                        await engine.generate_recommendations_for_user(user)
                        generated_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to generate recommendations for user {user.id}: {e}")

                logger.info(f"Periodic recommendations generation completed: {generated_count}/{len(users)} users")
                consecutive_failures = 0

        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic recommendations generation failed ({consecutive_failures}/{max_failures}): {e}")
            if consecutive_failures >= max_failures:
                logger.critical("Recommendations generation: Too many failures, backing off")
                await asyncio.sleep(SYNC_INTERVAL_RECOMMENDATIONS)
                consecutive_failures = 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    await init_db()
    logger.info("Database initialized")

    # Initialize Redis cache
    if await cache.connect():
        logger.info("Redis cache connected")
    else:
        logger.warning("Redis cache unavailable - running without caching")

    # Create shutdown event for graceful task termination
    shutdown_event = asyncio.Event()

    # Start periodic sync tasks with shutdown support
    kobo_sync_task = asyncio.create_task(
        periodic_kobo_sync(shutdown_event),
        name="kobo_sync"
    )
    letterboxd_sync_task = asyncio.create_task(
        periodic_letterboxd_sync(shutdown_event),
        name="letterboxd_sync"
    )
    streaming_links_task = asyncio.create_task(
        periodic_streaming_links_refresh(shutdown_event),
        name="streaming_links_refresh"
    )
    youtube_sync_task = asyncio.create_task(
        periodic_youtube_sync(shutdown_event),
        name="youtube_sync"
    )
    recommendations_task = asyncio.create_task(
        periodic_recommendations_generation(shutdown_event),
        name="recommendations_generation"
    )
    logger.info("Started periodic sync tasks (Kobo: 6h, Letterboxd: 12h, Streaming: 24h, YouTube: 6h, Recommendations: 12h)")

    yield

    # Graceful shutdown - signal all tasks to stop
    logger.info("Shutting down background tasks...")
    shutdown_event.set()

    # Close Redis cache connection
    await cache.close()
    logger.info("Redis cache closed")

    # Close persistent HTTP clients
    await close_all_clients()
    logger.info("HTTP clients closed")

    # Wait for tasks to complete gracefully (with timeout)
    tasks = [kobo_sync_task, letterboxd_sync_task, streaming_links_task, youtube_sync_task, recommendations_task]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0  # 10 second timeout for graceful shutdown
        )
        logger.info("All background tasks stopped gracefully")
    except TimeoutError:
        logger.warning("Background tasks did not stop in time, forcing cancellation")
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("Application shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware (order matters - first added = last executed)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MetricsMiddleware)  # Collect HTTP metrics
app.add_middleware(GZipMiddleware, minimum_size=500)  # Compress responses > 500 bytes

# CORS configuration for API access
if settings.is_development:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:8080"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Production: only allow same origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_url],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie=SESSION_COOKIE_NAME,
    max_age=60 * 60 * 24 * SESSION_TIMEOUT_DAYS,
    same_site="lax",
    https_only=settings.is_production,
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(api_router)
app.include_router(web_router)


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: Exception) -> RedirectResponse:
    """Redirect to login on 401."""
    return RedirectResponse(url="/login", status_code=302)


# Store app start time for uptime tracking
_app_start_time = datetime.now(UTC)


@app.get("/health", include_in_schema=True, tags=["monitoring"])
async def health_check() -> JSONResponse:
    """Health check endpoint for monitoring and load balancers.

    Returns:
        JSONResponse with status, uptime, and service health checks.
    """
    from sqlalchemy import text

    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(UTC).isoformat(),
        "uptime_seconds": (datetime.now(UTC) - _app_start_time).total_seconds(),
        "version": "0.1.0",
        "checks": {}
    }

    # Check database connection
    try:
        async with async_session_maker() as db:
            await db.execute(text("SELECT 1"))
        health_status["checks"]["database"] = {"status": "healthy"}
    except Exception:
        health_status["checks"]["database"] = {"status": "unhealthy"}
        health_status["status"] = "degraded"

    # Check Redis connection
    try:
        await cache.ping()
        health_status["checks"]["redis"] = {"status": "healthy"}
    except Exception:
        health_status["checks"]["redis"] = {"status": "unhealthy"}
        health_status["status"] = "degraded"

    status_code = 200 if health_status["status"] == "healthy" else 503
    return JSONResponse(content=health_status, status_code=status_code)


@app.get("/metrics", include_in_schema=True, tags=["monitoring"])
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint.

    Returns:
        Prometheus-formatted metrics text.
    """
    return Response(
        content=metrics.format_prometheus(),
        media_type="text/plain; charset=utf-8",
    )
