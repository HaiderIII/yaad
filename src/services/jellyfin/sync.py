"""Jellyfin bidirectional sync service.

Synchronizes media between Yaad and Jellyfin:
- Import: Jellyfin → Yaad (new items, watch status)
- Export: Yaad → Jellyfin (mark as watched)
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import async_session_maker
from src.models.media import Media, MediaStatus, MediaType
from src.models.schemas import MediaCreate, MediaStatusEnum, MediaTypeEnum
from src.models.user import User
from src.services.jellyfin.client import (
    JellyfinClient,
    JellyfinError,
    JellyfinItem,
    JellyfinMediaType,
    create_jellyfin_client,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    imported: int = 0
    updated: int = 0
    exported: int = 0
    skipped: int = 0
    errors: int = 0
    details: list[str] | None = None

    def __str__(self) -> str:
        return (
            f"imported={self.imported}, updated={self.updated}, "
            f"exported={self.exported}, skipped={self.skipped}, errors={self.errors}"
        )


# Type mapping: Jellyfin → Yaad
JELLYFIN_TO_YAAD_TYPE = {
    JellyfinMediaType.MOVIE: MediaType.FILM,
    JellyfinMediaType.SERIES: MediaType.SERIES,
    # Episodes are handled as part of series
}

# Type mapping: Yaad → Jellyfin
YAAD_TO_JELLYFIN_TYPE = {
    MediaType.FILM: JellyfinMediaType.MOVIE,
    MediaType.SERIES: JellyfinMediaType.SERIES,
}


class JellyfinSyncService:
    """Service for bidirectional sync with Jellyfin."""

    def __init__(self, client: JellyfinClient):
        self.client = client

    async def sync_from_jellyfin(
        self,
        db: AsyncSession,
        user_id: int,
        import_new: bool = True,
        update_existing: bool = True,
        media_types: list[JellyfinMediaType] | None = None,
    ) -> SyncResult:
        """Import media from Jellyfin to Yaad.

        Args:
            db: Database session
            user_id: Yaad user ID
            import_new: Import new items not in Yaad
            update_existing: Update watch status of existing items
            media_types: Types to sync (default: Movies and Series)

        Returns:
            SyncResult with counts
        """
        result = SyncResult(details=[])

        if not media_types:
            media_types = [JellyfinMediaType.MOVIE, JellyfinMediaType.SERIES]

        for media_type in media_types:
            try:
                items, total = await self.client.get_items(
                    media_type=media_type,
                    limit=1000,  # Get all items
                )
                logger.info(f"Found {total} {media_type.value} items in Jellyfin")

                for item in items:
                    try:
                        await self._process_jellyfin_item(
                            db=db,
                            user_id=user_id,
                            item=item,
                            import_new=import_new,
                            update_existing=update_existing,
                            result=result,
                        )
                    except Exception as e:
                        result.errors += 1
                        logger.error(f"Error processing {item.name}: {e}")

            except JellyfinError as e:
                result.errors += 1
                logger.error(f"Error fetching {media_type.value} from Jellyfin: {e}")

        await db.commit()
        return result

    async def _process_jellyfin_item(
        self,
        db: AsyncSession,
        user_id: int,
        item: JellyfinItem,
        import_new: bool,
        update_existing: bool,
        result: SyncResult,
    ) -> None:
        """Process a single Jellyfin item."""
        yaad_type = JELLYFIN_TO_YAAD_TYPE.get(item.type)
        if not yaad_type:
            result.skipped += 1
            return

        # Try to find existing media by Jellyfin ID or TMDB ID
        existing = await self._find_existing_media(
            db=db,
            user_id=user_id,
            jellyfin_id=item.id,
            tmdb_id=item.tmdb_id,
            media_type=yaad_type,
        )

        if existing:
            if update_existing:
                updated = await self._update_media_from_jellyfin(
                    db=db,
                    media=existing,
                    item=item,
                )
                if updated:
                    result.updated += 1
                    result.details.append(f"Updated: {item.name}")
            else:
                result.skipped += 1
        elif import_new:
            await self._create_media_from_jellyfin(
                db=db,
                user_id=user_id,
                item=item,
                yaad_type=yaad_type,
            )
            result.imported += 1
            result.details.append(f"Imported: {item.name}")
        else:
            result.skipped += 1

    async def _find_existing_media(
        self,
        db: AsyncSession,
        user_id: int,
        jellyfin_id: str,
        tmdb_id: str | None,
        media_type: MediaType,
    ) -> Media | None:
        """Find existing media by Jellyfin ID or TMDB ID."""
        # First try by Jellyfin ID
        query = select(Media).where(
            Media.user_id == user_id,
            Media.jellyfin_id == jellyfin_id,
        )
        result = await db.execute(query)
        media = result.scalar_one_or_none()
        if media:
            return media

        # Then try by TMDB ID
        if tmdb_id:
            query = select(Media).where(
                Media.user_id == user_id,
                Media.external_id == tmdb_id,
                Media.type == media_type,
            )
            result = await db.execute(query)
            media = result.scalar_one_or_none()
            if media:
                # Link Jellyfin ID
                media.jellyfin_id = jellyfin_id
                return media

        return None

    async def _update_media_from_jellyfin(
        self,
        db: AsyncSession,
        media: Media,
        item: JellyfinItem,
    ) -> bool:
        """Update existing media with Jellyfin data."""
        updated = False

        # Update watch status
        if item.played and media.status != MediaStatus.FINISHED:
            media.status = MediaStatus.FINISHED
            if not media.consumed_at:
                media.consumed_at = item.last_played_date or datetime.now(UTC)
            updated = True

        # Update Jellyfin sync metadata
        media.jellyfin_id = item.id
        media.jellyfin_etag = item.etag
        media.last_jellyfin_sync = datetime.now(UTC)

        return updated

    async def _create_media_from_jellyfin(
        self,
        db: AsyncSession,
        user_id: int,
        item: JellyfinItem,
        yaad_type: MediaType,
    ) -> Media:
        """Create new media from Jellyfin item."""
        status = MediaStatus.FINISHED if item.played else MediaStatus.TO_CONSUME

        media = Media(
            user_id=user_id,
            type=yaad_type,
            title=item.name,
            year=item.year,
            description=item.overview,
            duration_minutes=item.duration_minutes,
            status=status,
            external_id=item.tmdb_id,
            jellyfin_id=item.id,
            jellyfin_etag=item.etag,
            last_jellyfin_sync=datetime.now(UTC),
            consumed_at=item.last_played_date if item.played else None,
        )

        # Set cover URL from Jellyfin
        if item.image_tags and "Primary" in item.image_tags:
            media.cover_url = self.client.get_image_url(item.id)

        db.add(media)
        await db.flush()

        return media

    async def sync_to_jellyfin(
        self,
        db: AsyncSession,
        user_id: int,
        sync_watched: bool = True,
    ) -> SyncResult:
        """Export watch status from Yaad to Jellyfin.

        Args:
            db: Database session
            user_id: Yaad user ID
            sync_watched: Sync watched status to Jellyfin

        Returns:
            SyncResult with counts
        """
        result = SyncResult(details=[])

        if not sync_watched:
            return result

        # Get all finished media with Jellyfin IDs
        query = select(Media).where(
            Media.user_id == user_id,
            Media.jellyfin_id.isnot(None),
            Media.status == MediaStatus.FINISHED,
            Media.type.in_([MediaType.FILM, MediaType.SERIES]),
        )
        db_result = await db.execute(query)
        media_list = db_result.scalars().all()

        for media in media_list:
            try:
                # Check if already marked as played in Jellyfin
                jellyfin_item = await self.client.get_item(media.jellyfin_id)

                if jellyfin_item and not jellyfin_item.played:
                    await self.client.mark_played(media.jellyfin_id)
                    result.exported += 1
                    result.details.append(f"Marked as watched: {media.title}")
                    logger.info(f"Exported watch status for: {media.title}")
                else:
                    result.skipped += 1

            except JellyfinError as e:
                result.errors += 1
                logger.error(f"Error syncing {media.title} to Jellyfin: {e}")

        return result

    async def sync_bidirectional(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> dict[str, SyncResult]:
        """Perform full bidirectional sync.

        1. Import new items from Jellyfin
        2. Update watch status from Jellyfin
        3. Export watch status to Jellyfin

        Args:
            db: Database session
            user_id: Yaad user ID

        Returns:
            Dict with 'import' and 'export' SyncResults
        """
        logger.info(f"Starting bidirectional Jellyfin sync for user {user_id}")

        # Import from Jellyfin
        import_result = await self.sync_from_jellyfin(
            db=db,
            user_id=user_id,
            import_new=True,
            update_existing=True,
        )
        logger.info(f"Import complete: {import_result}")

        # Export to Jellyfin
        export_result = await self.sync_to_jellyfin(
            db=db,
            user_id=user_id,
            sync_watched=True,
        )
        logger.info(f"Export complete: {export_result}")

        return {
            "import": import_result,
            "export": export_result,
        }

    async def link_existing_media(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> SyncResult:
        """Link existing Yaad media to Jellyfin items by TMDB ID.

        Useful for initial setup when user already has media in Yaad.

        Args:
            db: Database session
            user_id: Yaad user ID

        Returns:
            SyncResult with counts
        """
        result = SyncResult(details=[])

        # Get media without Jellyfin ID but with TMDB ID
        query = select(Media).where(
            Media.user_id == user_id,
            Media.jellyfin_id.is_(None),
            Media.external_id.isnot(None),
            Media.type.in_([MediaType.FILM, MediaType.SERIES]),
        )
        db_result = await db.execute(query)
        media_list = db_result.scalars().all()

        for media in media_list:
            try:
                # Search in Jellyfin by TMDB ID
                jellyfin_type = YAAD_TO_JELLYFIN_TYPE.get(media.type)
                if not jellyfin_type:
                    continue

                jellyfin_item = await self.client.get_item_by_provider_id(
                    provider="Tmdb",
                    provider_id=media.external_id,
                    media_type=jellyfin_type,
                )

                if jellyfin_item:
                    media.jellyfin_id = jellyfin_item.id
                    media.jellyfin_etag = jellyfin_item.etag
                    media.last_jellyfin_sync = datetime.now(UTC)
                    result.updated += 1
                    result.details.append(f"Linked: {media.title}")
                    logger.info(f"Linked {media.title} to Jellyfin {jellyfin_item.id}")
                else:
                    result.skipped += 1

            except JellyfinError as e:
                result.errors += 1
                logger.error(f"Error linking {media.title}: {e}")

        await db.commit()
        return result


# ==================== Helper Functions ====================


async def get_jellyfin_client_for_user(user: User) -> JellyfinClient | None:
    """Create Jellyfin client for a user if configured.

    Args:
        user: User with Jellyfin settings

    Returns:
        JellyfinClient or None if not configured
    """
    if not user.jellyfin_url or not user.jellyfin_api_key:
        return None

    return create_jellyfin_client(
        server_url=user.jellyfin_url,
        api_key=user.jellyfin_api_key,
        user_id=user.jellyfin_user_id,
    )


async def sync_jellyfin_for_user(user: User) -> dict[str, Any] | None:
    """Run Jellyfin sync for a user.

    Args:
        user: User to sync

    Returns:
        Sync results or None if not configured
    """
    client = await get_jellyfin_client_for_user(user)
    if not client:
        return None

    if not user.jellyfin_sync_enabled:
        return {"status": "disabled", "message": "Jellyfin sync is disabled"}

    async with async_session_maker() as db:
        service = JellyfinSyncService(client)
        results = await service.sync_bidirectional(db, user.id)

        return {
            "status": "success",
            "import": {
                "imported": results["import"].imported,
                "updated": results["import"].updated,
                "errors": results["import"].errors,
            },
            "export": {
                "exported": results["export"].exported,
                "errors": results["export"].errors,
            },
        }


async def sync_all_jellyfin_users() -> dict[str, Any]:
    """Sync Jellyfin for all configured users.

    Returns:
        Summary of sync results
    """
    async with async_session_maker() as db:
        query = select(User).where(
            User.jellyfin_url.isnot(None),
            User.jellyfin_api_key.isnot(None),
            User.jellyfin_sync_enabled == True,
        )
        result = await db.execute(query)
        users = result.scalars().all()

        synced = 0
        errors = 0

        for user in users:
            try:
                sync_result = await sync_jellyfin_for_user(user)
                if sync_result and sync_result.get("status") == "success":
                    synced += 1
            except Exception as e:
                errors += 1
                logger.error(f"Jellyfin sync failed for user {user.id}: {e}")

        return {
            "total_users": len(users),
            "synced": synced,
            "errors": errors,
        }
