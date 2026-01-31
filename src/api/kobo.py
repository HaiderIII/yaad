"""Kobo integration API endpoints."""

import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.models.user import User
from src.services.kobo import KoboCredentials, kobo_client

router = APIRouter()
logger = logging.getLogger(__name__)


class ActivationResponse(BaseModel):
    """Response for starting activation."""

    activation_url: str
    user_code: str
    device_id: str
    polling_url: str


class CheckActivationRequest(BaseModel):
    """Request to check activation status."""

    device_id: str
    polling_url: str


class CheckActivationResponse(BaseModel):
    """Response for activation check."""

    complete: bool
    user_key: str | None = None


class CompleteActivationRequest(BaseModel):
    """Request to complete activation."""

    device_id: str
    user_key: str


class KoboStatusResponse(BaseModel):
    """Kobo connection status."""

    connected: bool
    device_id: str | None = None


class KoboBookResponse(BaseModel):
    """Book info from Kobo."""

    id: str
    title: str
    author: str | None
    isbn: str | None
    cover_url: str | None
    percent_read: float
    last_read: str | None
    is_finished: bool


class KoboLibraryResponse(BaseModel):
    """Kobo library sync response."""

    books: list[KoboBookResponse]
    total: int


# Store pending activations in session (device_id -> polling_url)
_pending_activations: dict[str, str] = {}


@router.get("/status", response_model=KoboStatusResponse)
async def get_kobo_status(
    user: Annotated[User, Depends(get_current_user)],
) -> KoboStatusResponse:
    """Get current Kobo connection status."""
    connected = bool(user.kobo_user_key and user.kobo_device_id)
    return KoboStatusResponse(
        connected=connected,
        device_id=user.kobo_device_id if connected else None,
    )


@router.post("/activate/start", response_model=ActivationResponse)
async def start_kobo_activation(
    user: Annotated[User, Depends(get_current_user)],
    request: Request,
) -> ActivationResponse:
    """Start Kobo device activation process.

    Returns an activation URL and 6-digit code for the user to enter at kobo.com/activate.
    """
    activation = await kobo_client.start_activation()

    if not activation:
        raise HTTPException(
            status_code=500,
            detail="Failed to start Kobo activation. Please try again.",
        )

    # Store in session for later
    request.session["kobo_activation"] = {
        "device_id": activation.device_id,
        "polling_url": activation.polling_url,
    }

    return ActivationResponse(
        activation_url=activation.activation_url,
        user_code=activation.user_code,  # 6-digit code, no formatting needed
        device_id=activation.device_id,
        polling_url=activation.polling_url,
    )


@router.post("/activate/check", response_model=CheckActivationResponse)
async def check_kobo_activation(
    body: CheckActivationRequest,
    user: Annotated[User, Depends(get_current_user)],
) -> CheckActivationResponse:
    """Check if the user has completed activation on kobo.com.

    Poll this endpoint until complete=true, then call /activate/complete.
    """
    result = await kobo_client.check_activation(
        device_id=body.device_id,
        polling_url=body.polling_url,
    )

    if result and result.get("complete"):
        return CheckActivationResponse(
            complete=True,
            user_key=result.get("user_key"),
        )

    return CheckActivationResponse(complete=False)


@router.post("/activate/complete", response_model=KoboStatusResponse)
async def complete_kobo_activation(
    body: CompleteActivationRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> KoboStatusResponse:
    """Complete Kobo activation after user has authorized.

    Authenticates the device and stores credentials.
    """
    # Authenticate device with Kobo
    credentials = await kobo_client.authenticate_device(
        device_id=body.device_id,
        user_key=body.user_key,
    )

    if not credentials:
        raise HTTPException(
            status_code=400,
            detail="Failed to authenticate with Kobo. Please try again.",
        )

    # Store credentials in user profile
    user.kobo_device_id = credentials.device_id
    user.kobo_user_key = json.dumps({
        "user_key": credentials.user_key,
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
    })

    await db.commit()

    return KoboStatusResponse(
        connected=True,
        device_id=credentials.device_id,
    )


@router.delete("/disconnect")
async def disconnect_kobo(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Disconnect Kobo account."""
    user.kobo_device_id = None
    user.kobo_user_key = None
    await db.commit()

    return {"success": True}


def _get_credentials(user: User) -> KoboCredentials | None:
    """Extract Kobo credentials from user profile."""
    if not user.kobo_device_id or not user.kobo_user_key:
        return None

    try:
        data = json.loads(user.kobo_user_key)
        return KoboCredentials(
            device_id=user.kobo_device_id,
            user_key=data.get("user_key", ""),
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
        )
    except (json.JSONDecodeError, KeyError):
        return None


@router.get("/library", response_model=KoboLibraryResponse)
async def get_kobo_library(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> KoboLibraryResponse:
    """Get user's Kobo library with reading progress."""
    credentials = _get_credentials(user)

    if not credentials:
        raise HTTPException(
            status_code=400,
            detail="Kobo not connected. Please connect your account first.",
        )

    # Validate and potentially refresh token
    if not await kobo_client.validate_credentials(credentials):
        new_credentials = await kobo_client.refresh_token(credentials)
        if not new_credentials:
            user.kobo_device_id = None
            user.kobo_user_key = None
            await db.commit()
            raise HTTPException(
                status_code=401,
                detail="Kobo session expired. Please reconnect your account.",
            )

        user.kobo_user_key = json.dumps({
            "user_key": new_credentials.user_key,
            "access_token": new_credentials.access_token,
            "refresh_token": new_credentials.refresh_token,
        })
        await db.commit()
        credentials = new_credentials

    books = await kobo_client.get_library(credentials)

    return KoboLibraryResponse(
        books=[
            KoboBookResponse(
                id=book.id,
                title=book.title,
                author=book.author,
                isbn=book.isbn,
                cover_url=book.cover_url,
                percent_read=book.percent_read,
                last_read=book.last_read.isoformat() if book.last_read else None,
                is_finished=book.is_finished,
            )
            for book in books
        ],
        total=len(books),
    )


@router.post("/sync")
async def sync_kobo_progress(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Sync reading progress from Kobo to Yaad library.

    - Updates progress for books already in Yaad
    - Imports new books/audiobooks from Kobo that aren't in Yaad yet
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from src.models.book import BookMetadata
    from src.models.media import Author, Media, MediaStatus, MediaType, OwnershipType

    credentials = _get_credentials(user)
    if not credentials:
        raise HTTPException(
            status_code=400,
            detail="Kobo not connected",
        )

    kobo_books = await kobo_client.get_library(credentials)

    if not kobo_books:
        return {"synced": 0, "imported": 0, "message": "No books found in Kobo library"}

    synced = 0
    imported = 0

    for kobo_book in kobo_books:
        # Skip books without proper metadata (title "Unknown")
        if kobo_book.title == "Unknown" or not kobo_book.title:
            continue

        media = None

        # Try to match by kobo_id first (most reliable)
        query = (
            select(Media)
            .join(BookMetadata, Media.id == BookMetadata.media_id)
            .options(selectinload(Media.book_metadata))
            .where(
                Media.user_id == user.id,
                Media.type == MediaType.BOOK,
                BookMetadata.kobo_id == kobo_book.id,
            )
        )
        result = await db.execute(query)
        media = result.scalar_one_or_none()

        # Try to match by ISBN
        if not media and kobo_book.isbn:
            query = (
                select(Media)
                .join(BookMetadata, Media.id == BookMetadata.media_id)
                .options(selectinload(Media.book_metadata))
                .where(
                    Media.user_id == user.id,
                    Media.type == MediaType.BOOK,
                    BookMetadata.isbn == kobo_book.isbn,
                )
            )
            result = await db.execute(query)
            media = result.scalar_one_or_none()

        # Fallback to exact title matching
        if not media:
            query = (
                select(Media)
                .options(selectinload(Media.book_metadata))
                .where(
                    Media.user_id == user.id,
                    Media.type == MediaType.BOOK,
                    Media.title == kobo_book.title,
                )
            )
            result = await db.execute(query)
            media = result.scalar_one_or_none()

        if media:
            # Update existing book
            if kobo_book.percent_read > 0:
                if media.page_count:
                    media.current_page = int(media.page_count * kobo_book.percent_read / 100)

            if kobo_book.is_finished and media.status != MediaStatus.FINISHED:
                media.status = MediaStatus.FINISHED
                if kobo_book.last_read:
                    media.consumed_at = kobo_book.last_read
            elif kobo_book.percent_read > 0 and media.status == MediaStatus.TO_CONSUME:
                media.status = MediaStatus.IN_PROGRESS

            # Update kobo_id in book_metadata if not set
            if media.book_metadata and not media.book_metadata.kobo_id:
                media.book_metadata.kobo_id = kobo_book.id
            if media.book_metadata:
                media.book_metadata.progress_percent = kobo_book.percent_read

            # Always enrich book metadata via ISBN using Google Books / Open Library
            isbn = kobo_book.isbn or (media.book_metadata.isbn if media.book_metadata else None)

            if isbn:
                from src.services.metadata.books import book_service

                enriched_data = await book_service.search_by_isbn(isbn)
                if enriched_data:
                    # Always update with enriched data (overwrite existing)
                    if enriched_data.get("cover_url"):
                        media.cover_url = enriched_data["cover_url"]
                    if enriched_data.get("page_count"):
                        media.page_count = enriched_data["page_count"]
                    if enriched_data.get("description"):
                        # Strip HTML tags from description
                        media.description = re.sub(r'<[^>]+>', '', enriched_data["description"])
                    if enriched_data.get("year"):
                        media.year = enriched_data["year"]
                    if media.book_metadata:
                        if not media.book_metadata.isbn:
                            media.book_metadata.isbn = isbn
                        if enriched_data.get("publisher"):
                            media.book_metadata.publisher = enriched_data["publisher"]

            synced += 1
        else:
            # Import new book from Kobo
            # Try to enrich with Google Books / Open Library for better cover
            from src.services.metadata.books import book_service

            enriched_data: dict | None = None
            if kobo_book.isbn:
                enriched_data = await book_service.search_by_isbn(kobo_book.isbn)

            # Use enriched data if available, fallback to Kobo data
            cover_url = (
                (enriched_data.get("cover_url") if enriched_data else None)
                or kobo_book.cover_url
            )
            raw_description = (
                kobo_book.description
                or (enriched_data.get("description") if enriched_data else None)
            )
            # Strip HTML tags from description
            description = re.sub(r'<[^>]+>', '', raw_description) if raw_description else None
            page_count = enriched_data.get("page_count") if enriched_data else None
            year = enriched_data.get("year") if enriched_data else None

            # Get or create author (prefer Kobo author name)
            author_name = kobo_book.author
            if not author_name and enriched_data and enriched_data.get("authors"):
                author_name = enriched_data["authors"][0]

            authors = []
            if author_name:
                author_query = select(Author).where(
                    Author.name == author_name,
                    Author.media_type == MediaType.BOOK,
                )
                author_result = await db.execute(author_query)
                author = author_result.scalar_one_or_none()

                if not author:
                    author = Author(name=author_name, media_type=MediaType.BOOK)
                    db.add(author)
                    await db.flush()

                authors = [author]

            # Determine status based on progress
            if kobo_book.is_finished:
                status = MediaStatus.FINISHED
            elif kobo_book.percent_read > 0:
                status = MediaStatus.IN_PROGRESS
            else:
                status = MediaStatus.TO_CONSUME

            # Create new media entry
            new_media = Media(
                user_id=user.id,
                type=MediaType.BOOK,
                title=kobo_book.title,
                description=description,
                cover_url=cover_url,
                page_count=page_count,
                year=year,
                status=status,
                consumed_at=kobo_book.last_read if kobo_book.is_finished else None,
                ownership_type=OwnershipType.EBOOK,
                authors=authors,
            )
            db.add(new_media)
            await db.flush()

            # Create book metadata
            book_metadata = BookMetadata(
                media_id=new_media.id,
                kobo_id=kobo_book.id,
                isbn=kobo_book.isbn,
                publisher=kobo_book.publisher or (enriched_data.get("publisher") if enriched_data else None),
                progress_percent=kobo_book.percent_read,
            )
            db.add(book_metadata)

            imported += 1

    await db.commit()

    message_parts = []
    if synced > 0:
        message_parts.append(f"Updated {synced} existing books")
    if imported > 0:
        message_parts.append(f"Imported {imported} new books")

    message = ". ".join(message_parts) if message_parts else "No changes needed"

    return {
        "synced": synced,
        "imported": imported,
        "total_kobo_books": len(kobo_books),
        "message": message,
    }
