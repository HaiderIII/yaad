"""User API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.models.user import User
from src.services.metadata.tmdb import tmdb_service

router = APIRouter()


class StreamingPreferencesUpdate(BaseModel):
    """Schema for updating streaming preferences."""

    country: str | None = None
    streaming_platforms: list[int] | None = None


class StreamingPreferencesResponse(BaseModel):
    """Schema for streaming preferences response."""

    country: str
    streaming_platforms: list[int]


class ProviderInfo(BaseModel):
    """Schema for provider info."""

    provider_id: int
    provider_name: str
    logo_path: str | None


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

    await db.commit()
    await db.refresh(user)

    return StreamingPreferencesResponse(
        country=user.country,
        streaming_platforms=user.streaming_platforms or [],
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
