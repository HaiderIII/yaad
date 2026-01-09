"""Main FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.api import api_router
from src.config import get_settings
from src.db import init_db
from src.web import web_router

settings = get_settings()
logger = logging.getLogger(__name__)

# Sync intervals
KOBO_SYNC_INTERVAL = 6 * 60 * 60  # 6 hours
LETTERBOXD_SYNC_INTERVAL = 12 * 60 * 60  # 12 hours (RSS-based, lighter)


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
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        # HSTS (only in production)
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


async def periodic_kobo_sync() -> None:
    """Background task that syncs Kobo for all connected users periodically."""
    while True:
        await asyncio.sleep(KOBO_SYNC_INTERVAL)
        try:
            from src.services.kobo.sync import sync_all_kobo_users

            result = await sync_all_kobo_users()
            logger.info(f"Periodic Kobo sync completed: {result}")
        except Exception as e:
            logger.error(f"Periodic Kobo sync failed: {e}")


async def periodic_letterboxd_sync() -> None:
    """Background task that syncs Letterboxd for all configured users periodically."""
    while True:
        await asyncio.sleep(LETTERBOXD_SYNC_INTERVAL)
        try:
            from src.services.imports.sync import sync_all_letterboxd_users

            result = await sync_all_letterboxd_users(full_import=False)
            logger.info(f"Periodic Letterboxd sync completed: {result}")
        except Exception as e:
            logger.error(f"Periodic Letterboxd sync failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    await init_db()

    # Start periodic sync tasks
    kobo_sync_task = asyncio.create_task(periodic_kobo_sync())
    letterboxd_sync_task = asyncio.create_task(periodic_letterboxd_sync())
    logger.info("Started periodic sync tasks (Kobo: 6h, Letterboxd: 12h)")

    yield

    # Shutdown - cancel the background tasks
    kobo_sync_task.cancel()
    letterboxd_sync_task.cancel()
    try:
        await kobo_sync_task
    except asyncio.CancelledError:
        pass
    try:
        await letterboxd_sync_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware (order matters - first added = last executed)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)  # Compress responses > 500 bytes
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie="yaad_session",
    max_age=60 * 60 * 24 * 7,  # 7 days
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
