"""Authentication API endpoints."""

import logging
import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.config import get_settings
from src.constants import HTTPX_TIMEOUT
from src.db import get_db
from src.models.user import User

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

# GitHub OAuth URLs
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# Google OAuth URLs
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


# ============== OAuth Helpers ==============


async def _exchange_token_and_get_user(
    token_url: str,
    user_url: str,
    token_data: dict[str, str],
) -> dict[str, Any]:
    """Exchange OAuth code for token and fetch user info."""
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
        # Exchange code for token
        token_response = await client.post(
            token_url,
            data=token_data,
            headers={"Accept": "application/json"},
        )

        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get access token")

        token_result = token_response.json()
        if "error" in token_result:
            raise HTTPException(
                status_code=400, detail=token_result.get("error_description", "OAuth error")
            )

        access_token = token_result.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Get user info
        user_response = await client.get(
            user_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

        if user_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info")

        return user_response.json()


async def _get_or_link_user_by_email(
    db: AsyncSession,
    email: str | None,
    provider_id_field: str,
    provider_id: str | int,
) -> User | None:
    """Try to find existing user by email and link provider account."""
    if not email:
        return None
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user:
        setattr(user, provider_id_field, provider_id)
    return user


def _set_session_and_redirect(
    request: Request, user: User, background_tasks: BackgroundTasks
) -> RedirectResponse:
    """Set user session, trigger background syncs, and redirect to home."""
    request.session["user_id"] = user.id
    request.session["username"] = user.username

    # Trigger Kobo sync in background if connected
    if user.kobo_device_id and user.kobo_user_key:
        background_tasks.add_task(_run_kobo_sync, user.id)

    # Trigger Letterboxd sync in background if configured
    if user.letterboxd_username:
        background_tasks.add_task(_run_letterboxd_sync, user.id)

    return RedirectResponse(url="/", status_code=302)


async def _run_kobo_sync(user_id: int) -> None:
    """Run Kobo sync in background."""
    from src.services.kobo.sync import sync_kobo_for_user_id

    try:
        result = await sync_kobo_for_user_id(user_id)
        logger.info(f"Background Kobo sync for user {user_id}: {result}")
    except Exception as e:
        logger.error(f"Background Kobo sync failed for user {user_id}: {e}")


async def _run_letterboxd_sync(user_id: int) -> None:
    """Run Letterboxd sync in background (RSS only for speed)."""
    from src.services.imports.sync import sync_letterboxd_for_user_id

    try:
        result = await sync_letterboxd_for_user_id(user_id, full_import=False)
        logger.info(f"Background Letterboxd sync for user {user_id}: {result}")
    except Exception as e:
        logger.error(f"Background Letterboxd sync failed for user {user_id}: {e}")


# ============== GitHub OAuth ==============

@router.get("/github/login")
async def github_login(request: Request) -> RedirectResponse:
    """Initiate GitHub OAuth login."""
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_provider"] = "github"

    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": f"{settings.app_url}/api/auth/github/callback",
        "scope": "user:email",
        "state": state,
    }
    url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str,
    state: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """Handle GitHub OAuth callback."""
    # Verify state - if invalid, clear session and restart OAuth flow
    stored_state = request.session.get("oauth_state")
    if not stored_state or stored_state != state:
        request.session.clear()
        return RedirectResponse(url="/api/auth/github/login", status_code=302)

    del request.session["oauth_state"]
    del request.session["oauth_provider"]

    # Exchange code for token and get user info
    github_user = await _exchange_token_and_get_user(
        token_url=GITHUB_TOKEN_URL,
        user_url=GITHUB_USER_URL,
        token_data={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": f"{settings.app_url}/api/auth/github/callback",
        },
    )

    # Get or create user
    result = await db.execute(select(User).where(User.github_id == github_user["id"]))
    user = result.scalar_one_or_none()

    if not user:
        # Try to link by email
        user = await _get_or_link_user_by_email(
            db, github_user.get("email"), "github_id", github_user["id"]
        )

    if not user:
        user = User(
            github_id=github_user["id"],
            username=github_user["login"],
            email=github_user.get("email"),
            avatar_url=github_user.get("avatar_url"),
        )
        db.add(user)
        await db.flush()
    else:
        # Update user info
        user.avatar_url = github_user.get("avatar_url")
        if github_user.get("email") and not user.email:
            user.email = github_user["email"]

    await db.commit()
    return _set_session_and_redirect(request, user, background_tasks)


# ============== Google OAuth ==============

@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
    """Initiate Google OAuth login."""
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_provider"] = "google"

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.app_url}/api/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str,
    state: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """Handle Google OAuth callback."""
    # Verify state - if invalid, clear session and restart OAuth flow
    stored_state = request.session.get("oauth_state")
    if not stored_state or stored_state != state:
        request.session.clear()
        return RedirectResponse(url="/api/auth/google/login", status_code=302)

    del request.session["oauth_state"]
    del request.session["oauth_provider"]

    # Exchange code for token and get user info
    google_user = await _exchange_token_and_get_user(
        token_url=GOOGLE_TOKEN_URL,
        user_url=GOOGLE_USER_URL,
        token_data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "redirect_uri": f"{settings.app_url}/api/auth/google/callback",
            "grant_type": "authorization_code",
        },
    )

    # Get or create user
    result = await db.execute(select(User).where(User.google_id == google_user["id"]))
    user = result.scalar_one_or_none()

    if not user:
        # Try to link by email
        user = await _get_or_link_user_by_email(
            db, google_user.get("email"), "google_id", google_user["id"]
        )

    if not user:
        # Generate username from email or name
        username = google_user.get("email", "").split("@")[0]
        if not username:
            username = google_user.get("name", "user").replace(" ", "_").lower()

        # Ensure unique username
        base_username = username
        counter = 1
        while True:
            result = await db.execute(select(User).where(User.username == username))
            if not result.scalar_one_or_none():
                break
            username = f"{base_username}_{counter}"
            counter += 1

        user = User(
            google_id=google_user["id"],
            username=username,
            email=google_user.get("email"),
            avatar_url=google_user.get("picture"),
        )
        db.add(user)
        await db.flush()
    else:
        # Update user info
        if google_user.get("picture"):
            user.avatar_url = google_user["picture"]
        if google_user.get("email") and not user.email:
            user.email = google_user["email"]

    await db.commit()
    return _set_session_and_redirect(request, user, background_tasks)


# ============== Common ==============

@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Log out the current user."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@router.get("/me")
async def get_me(user: Annotated[User, Depends(get_current_user)]) -> dict:
    """Get current authenticated user."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "avatar_url": user.avatar_url,
        "locale": user.locale,
    }
