"""Kobo sync background service."""

import json
import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.book import BookMetadata
from src.models.media import Author, Media, MediaStatus, MediaType, OwnershipType
from src.models.user import User
from src.services.kobo.client import KoboCredentials, kobo_client

logger = logging.getLogger(__name__)


def get_credentials(user: User) -> KoboCredentials | None:
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


async def sync_user_kobo(db: AsyncSession, user: User) -> dict:
    """Sync Kobo library for a single user.

    Returns dict with synced/imported counts.
    """
    credentials = get_credentials(user)
    if not credentials:
        return {"synced": 0, "imported": 0, "skipped": True, "reason": "not_connected"}

    # Validate and potentially refresh token
    if not await kobo_client.validate_credentials(credentials):
        new_credentials = await kobo_client.refresh_token(credentials)
        if not new_credentials:
            # Clear invalid credentials
            user.kobo_device_id = None
            user.kobo_user_key = None
            await db.commit()
            return {"synced": 0, "imported": 0, "skipped": True, "reason": "token_expired"}

        user.kobo_user_key = json.dumps({
            "user_key": new_credentials.user_key,
            "access_token": new_credentials.access_token,
            "refresh_token": new_credentials.refresh_token,
        })
        await db.commit()
        credentials = new_credentials

    kobo_books = await kobo_client.get_library(credentials)

    if not kobo_books:
        return {"synced": 0, "imported": 0, "message": "No books in Kobo library"}

    synced = 0
    imported = 0

    for kobo_book in kobo_books:
        # Skip books without proper metadata
        if kobo_book.title == "Unknown" or not kobo_book.title:
            continue

        media = None

        # Try to match by kobo_id first
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

            # Enrich via ISBN
            isbn = kobo_book.isbn or (media.book_metadata.isbn if media.book_metadata else None)
            if isbn:
                from src.services.metadata.books import book_service
                enriched_data = await book_service.search_by_isbn(isbn)
                if enriched_data:
                    if enriched_data.get("cover_url"):
                        media.cover_url = enriched_data["cover_url"]
                    if enriched_data.get("page_count"):
                        media.page_count = enriched_data["page_count"]
                    if enriched_data.get("description"):
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
            from src.services.metadata.books import book_service

            enriched_data: dict | None = None
            if kobo_book.isbn:
                enriched_data = await book_service.search_by_isbn(kobo_book.isbn)

            cover_url = (
                (enriched_data.get("cover_url") if enriched_data else None)
                or kobo_book.cover_url
            )
            raw_description = (
                kobo_book.description
                or (enriched_data.get("description") if enriched_data else None)
            )
            description = re.sub(r'<[^>]+>', '', raw_description) if raw_description else None
            page_count = enriched_data.get("page_count") if enriched_data else None
            year = enriched_data.get("year") if enriched_data else None

            # Get or create author
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

            # Determine status
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

    return {
        "synced": synced,
        "imported": imported,
        "total_kobo_books": len(kobo_books),
    }


async def sync_kobo_for_user_id(user_id: int) -> dict:
    """Sync Kobo for a user by ID (for background tasks)."""
    from src.db import async_session_maker

    async with async_session_maker() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            return {"error": "User not found"}

        return await sync_user_kobo(db, user)


async def sync_all_kobo_users() -> dict:
    """Sync Kobo for all connected users (for periodic job)."""
    from src.db import async_session_maker

    async with async_session_maker() as db:
        # Get all users with Kobo connected
        result = await db.execute(
            select(User).where(
                User.kobo_device_id.isnot(None),
                User.kobo_user_key.isnot(None),
            )
        )
        users = result.scalars().all()

        total_synced = 0
        total_imported = 0
        users_processed = 0

        for user in users:
            try:
                result = await sync_user_kobo(db, user)
                if not result.get("skipped"):
                    total_synced += result.get("synced", 0)
                    total_imported += result.get("imported", 0)
                    users_processed += 1
            except Exception as e:
                logger.error(f"Failed to sync Kobo for user {user.id}: {e}")

        logger.info(
            f"Kobo sync completed: {users_processed} users, "
            f"{total_synced} synced, {total_imported} imported"
        )

        return {
            "users_processed": users_processed,
            "total_synced": total_synced,
            "total_imported": total_imported,
        }
