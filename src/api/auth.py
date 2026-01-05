"""Authentication API endpoints."""

import secrets
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.config import get_settings
from src.db import get_db
from src.models.user import User

router = APIRouter()
settings = get_settings()

# GitHub OAuth URLs
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# Google OAuth URLs
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


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
) -> RedirectResponse:
    """Handle GitHub OAuth callback."""
    # Verify state - if invalid, clear session and restart OAuth flow
    stored_state = request.session.get("oauth_state")
    if not stored_state or stored_state != state:
        # Session is corrupted or expired - clear and restart OAuth flow
        # This avoids the "double-click" issue by automatically retrying
        request.session.clear()
        return RedirectResponse(url="/api/auth/github/login", status_code=302)

    del request.session["oauth_state"]
    del request.session["oauth_provider"]

    # Exchange code for token
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": f"{settings.app_url}/api/auth/github/callback",
            },
            headers={"Accept": "application/json"},
        )

        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get access token")

        token_data = token_response.json()
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "OAuth error"))

        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Get user info
        user_response = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

        if user_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info")

        github_user = user_response.json()

    # Get or create user
    result = await db.execute(select(User).where(User.github_id == github_user["id"]))
    user = result.scalar_one_or_none()

    if not user:
        # Check if user exists with same email (link accounts)
        if github_user.get("email"):
            result = await db.execute(select(User).where(User.email == github_user["email"]))
            user = result.scalar_one_or_none()
            if user:
                user.github_id = github_user["id"]

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

    # Set session
    request.session["user_id"] = user.id
    request.session["username"] = user.username

    return RedirectResponse(url="/", status_code=302)


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
) -> RedirectResponse:
    """Handle Google OAuth callback."""
    # Verify state - if invalid, clear session and restart OAuth flow
    stored_state = request.session.get("oauth_state")
    if not stored_state or stored_state != state:
        # Session is corrupted or expired - clear and restart OAuth flow
        # This avoids the "double-click" issue by automatically retrying
        request.session.clear()
        return RedirectResponse(url="/api/auth/google/login", status_code=302)

    del request.session["oauth_state"]
    del request.session["oauth_provider"]

    # Exchange code for token
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "redirect_uri": f"{settings.app_url}/api/auth/google/callback",
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )

        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get access token")

        token_data = token_response.json()
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "OAuth error"))

        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Get user info
        user_response = await client.get(
            GOOGLE_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

        if user_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info")

        google_user = user_response.json()

    # Get or create user
    result = await db.execute(select(User).where(User.google_id == google_user["id"]))
    user = result.scalar_one_or_none()

    if not user:
        # Check if user exists with same email (link accounts)
        if google_user.get("email"):
            result = await db.execute(select(User).where(User.email == google_user["email"]))
            user = result.scalar_one_or_none()
            if user:
                user.google_id = google_user["id"]

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

    # Set session
    request.session["user_id"] = user.id
    request.session["username"] = user.username

    return RedirectResponse(url="/", status_code=302)


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
