"""User API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.models.media import BookLocation
from src.models.user import User
from src.services.metadata.tmdb import tmdb_service

router = APIRouter()


class LocaleUpdate(BaseModel):
    """Schema for updating user locale."""

    locale: str


class LocaleResponse(BaseModel):
    """Schema for locale response."""

    locale: str


class StreamingPreferencesUpdate(BaseModel):
    """Schema for updating streaming preferences."""

    country: str | None = None
    streaming_platforms: list[int] | None = None
    letterboxd_username: str | None = None


class UserSettingsUpdate(BaseModel):
    """Schema for updating user settings (JSON field)."""

    settings: dict | None = None


class UserSettingsResponse(BaseModel):
    """Schema for user settings response."""

    settings: dict


class StreamingPreferencesResponse(BaseModel):
    """Schema for streaming preferences response."""

    country: str
    streaming_platforms: list[int]
    letterboxd_username: str | None = None


class ProviderInfo(BaseModel):
    """Schema for provider info."""

    provider_id: int
    provider_name: str
    logo_path: str | None


SUPPORTED_LOCALES = {"en", "fr"}


@router.patch("/locale", response_model=LocaleResponse)
async def update_locale(
    data: LocaleUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocaleResponse:
    """Update user's locale preference."""
    if data.locale not in SUPPORTED_LOCALES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported locale. Supported: {', '.join(SUPPORTED_LOCALES)}",
        )

    user.locale = data.locale
    await db.commit()
    await db.refresh(user)

    return LocaleResponse(locale=user.locale)


@router.get("/locale", response_model=LocaleResponse)
async def get_locale(
    user: Annotated[User, Depends(get_current_user)],
) -> LocaleResponse:
    """Get user's locale preference."""
    return LocaleResponse(locale=user.locale)


@router.get("/streaming-preferences", response_model=StreamingPreferencesResponse)
async def get_streaming_preferences(
    user: Annotated[User, Depends(get_current_user)],
) -> StreamingPreferencesResponse:
    """Get user's streaming preferences."""
    return StreamingPreferencesResponse(
        country=user.country,
        streaming_platforms=user.streaming_platforms or [],
    )


@router.patch("/streaming-preferences", response_model=StreamingPreferencesResponse)
async def update_streaming_preferences(
    data: StreamingPreferencesUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingPreferencesResponse:
    """Update user's streaming preferences."""
    if data.country is not None:
        # Validate country code (2 letter ISO code)
        if len(data.country) != 2:
            raise HTTPException(status_code=400, detail="Country must be a 2-letter ISO code")
        user.country = data.country.upper()

    if data.streaming_platforms is not None:
        user.streaming_platforms = data.streaming_platforms

    # Handle letterboxd_username - can be set to None to disconnect
    if "letterboxd_username" in data.model_fields_set:
        user.letterboxd_username = data.letterboxd_username

    await db.commit()
    await db.refresh(user)

    return StreamingPreferencesResponse(
        country=user.country,
        streaming_platforms=user.streaming_platforms or [],
        letterboxd_username=user.letterboxd_username,
    )


@router.get("/available-providers", response_model=list[ProviderInfo])
async def get_available_providers(
    user: Annotated[User, Depends(get_current_user)],
    country: str | None = None,
) -> list[ProviderInfo]:
    """Get available streaming providers for a country.

    If no country is specified, uses the user's configured country.
    """
    target_country = country or user.country

    providers = await tmdb_service.get_available_providers(target_country)

    return [
        ProviderInfo(
            provider_id=p["provider_id"],
            provider_name=p["provider_name"],
            logo_path=p["logo_path"],
        )
        for p in providers[:50]  # Limit to top 50 providers
    ]


@router.get("/settings", response_model=UserSettingsResponse)
async def get_user_settings(
    user: Annotated[User, Depends(get_current_user)],
) -> UserSettingsResponse:
    """Get user's settings (JSON field)."""
    return UserSettingsResponse(settings=user.settings or {})


@router.patch("/settings", response_model=UserSettingsResponse)
async def update_user_settings(
    data: UserSettingsUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserSettingsResponse:
    """Update user's settings (JSON field).

    Replaces the entire settings object with the provided one.
    """
    if data.settings is not None:
        user.settings = data.settings

    await db.commit()
    await db.refresh(user)

    return UserSettingsResponse(settings=user.settings or {})


# Book Location schemas (defined here for simplicity)
class BookLocationCreate(BaseModel):
    """Schema for creating a book location."""

    name: str


class BookLocationResponse(BaseModel):
    """Schema for book location response."""

    id: int
    name: str


@router.get("/book-locations", response_model=list[BookLocationResponse])
async def get_book_locations(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BookLocationResponse]:
    """Get user's book storage locations."""
    from sqlalchemy import select

    result = await db.execute(
        select(BookLocation).where(BookLocation.user_id == user.id).order_by(BookLocation.name)
    )
    locations = result.scalars().all()
    return [BookLocationResponse(id=loc.id, name=loc.name) for loc in locations]


@router.post("/book-locations", response_model=BookLocationResponse, status_code=201)
async def create_book_location(
    data: BookLocationCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BookLocationResponse:
    """Create a new book storage location."""
    from sqlalchemy import select

    # Check if location with same name already exists
    result = await db.execute(
        select(BookLocation).where(
            BookLocation.user_id == user.id, BookLocation.name == data.name
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Location with this name already exists")

    location = BookLocation(user_id=user.id, name=data.name)
    db.add(location)
    await db.commit()
    await db.refresh(location)

    return BookLocationResponse(id=location.id, name=location.name)


@router.delete("/book-locations/{location_id}", status_code=204)
async def delete_book_location(
    location_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a book storage location."""
    from sqlalchemy import select

    result = await db.execute(
        select(BookLocation).where(
            BookLocation.id == location_id, BookLocation.user_id == user.id
        )
    )
    location = result.scalar_one_or_none()
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    await db.delete(location)
    await db.commit()
