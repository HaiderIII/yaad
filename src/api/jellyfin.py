"""Jellyfin integration API endpoints."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import get_current_user
from src.db import get_db
from src.models.user import User
from src.services.jellyfin.client import (
    JellyfinAuthError,
    JellyfinConnectionError,
    JellyfinError,
    create_jellyfin_client,
)
from src.services.jellyfin.sync import (
    JellyfinSyncService,
    get_jellyfin_client_for_user,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ==================== Request/Response Models ====================


class JellyfinConnectRequest(BaseModel):
    """Request to connect Jellyfin server."""

    server_url: str = Field(..., description="Jellyfin server URL (e.g., http://localhost:8096)")
    api_key: str = Field(..., description="API key from Jellyfin dashboard")
    user_id: str | None = Field(None, description="Jellyfin user ID (optional, can be set later)")


class JellyfinUserSelect(BaseModel):
    """Request to select Jellyfin user."""

    user_id: str = Field(..., description="Jellyfin user ID")


class JellyfinSettingsUpdate(BaseModel):
    """Request to update Jellyfin settings."""

    sync_enabled: bool | None = Field(None, description="Enable automatic sync")


class JellyfinStatusResponse(BaseModel):
    """Response with Jellyfin connection status."""

    connected: bool
    server_url: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    sync_enabled: bool = False
    error: str | None = None


class JellyfinUserResponse(BaseModel):
    """Jellyfin user info."""

    id: str
    name: str


class JellyfinSyncResponse(BaseModel):
    """Response from sync operation."""

    status: str
    import_result: dict[str, int] | None = None
    export_result: dict[str, int] | None = None
    message: str | None = None


# ==================== Endpoints ====================


@router.get("/status", response_model=JellyfinStatusResponse)
async def get_jellyfin_status(
    user: Annotated[User, Depends(get_current_user)],
) -> JellyfinStatusResponse:
    """Get Jellyfin connection status."""
    if not user.jellyfin_url or not user.jellyfin_api_key:
        return JellyfinStatusResponse(connected=False)

    try:
        client = await get_jellyfin_client_for_user(user)
        if not client:
            return JellyfinStatusResponse(connected=False)

        # Test connection and get server info
        success, message = await client.test_connection()

        if not success:
            return JellyfinStatusResponse(
                connected=False,
                server_url=user.jellyfin_url,
                error=message,
            )

        # Get server info
        server_info = await client.get_server_info()

        # Get user info if user_id is set
        user_name = None
        if user.jellyfin_user_id:
            client.user_id = user.jellyfin_user_id
            try:
                jf_user = await client.get_current_user()
                if jf_user:
                    user_name = jf_user.name
            except JellyfinError:
                pass

        return JellyfinStatusResponse(
            connected=True,
            server_url=user.jellyfin_url,
            server_name=server_info.get("ServerName"),
            server_version=server_info.get("Version"),
            user_id=user.jellyfin_user_id,
            user_name=user_name,
            sync_enabled=user.jellyfin_sync_enabled,
        )

    except Exception as e:
        logger.error(f"Error checking Jellyfin status: {e}")
        return JellyfinStatusResponse(
            connected=False,
            server_url=user.jellyfin_url,
            error=str(e),
        )


@router.post("/connect", response_model=JellyfinStatusResponse)
async def connect_jellyfin(
    request: JellyfinConnectRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JellyfinStatusResponse:
    """Connect to a Jellyfin server."""
    # Test connection first
    client = create_jellyfin_client(
        server_url=request.server_url,
        api_key=request.api_key,
        user_id=request.user_id,
    )

    try:
        success, message = await client.test_connection()
        if not success:
            raise HTTPException(status_code=400, detail=message)

        # Get server info
        server_info = await client.get_server_info()

        # Save credentials
        user.jellyfin_url = request.server_url
        user.jellyfin_api_key = request.api_key
        user.jellyfin_user_id = request.user_id
        await db.commit()

        logger.info(f"User {user.id} connected to Jellyfin: {request.server_url}")

        return JellyfinStatusResponse(
            connected=True,
            server_url=request.server_url,
            server_name=server_info.get("ServerName"),
            server_version=server_info.get("Version"),
            user_id=request.user_id,
            sync_enabled=user.jellyfin_sync_enabled,
        )

    except JellyfinAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except JellyfinConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except JellyfinError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/disconnect")
async def disconnect_jellyfin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Disconnect from Jellyfin server."""
    user.jellyfin_url = None
    user.jellyfin_api_key = None
    user.jellyfin_user_id = None
    user.jellyfin_sync_enabled = False
    await db.commit()

    logger.info(f"User {user.id} disconnected from Jellyfin")

    return {"status": "disconnected", "message": "Jellyfin disconnected successfully"}


@router.get("/users", response_model=list[JellyfinUserResponse])
async def get_jellyfin_users(
    user: Annotated[User, Depends(get_current_user)],
) -> list[JellyfinUserResponse]:
    """Get available Jellyfin users."""
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    try:
        users = await client.get_users()
        return [
            JellyfinUserResponse(id=u.id, name=u.name)
            for u in users
        ]
    except JellyfinAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except JellyfinError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users/select")
async def select_jellyfin_user(
    request: JellyfinUserSelect,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Select which Jellyfin user to sync with."""
    if not user.jellyfin_url:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    # Verify user exists
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    try:
        users = await client.get_users()
        if not any(u.id == request.user_id for u in users):
            raise HTTPException(status_code=404, detail="Jellyfin user not found")

        user.jellyfin_user_id = request.user_id
        await db.commit()

        return {"status": "success", "message": f"Selected Jellyfin user: {request.user_id}"}

    except JellyfinError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/settings")
async def update_jellyfin_settings(
    request: JellyfinSettingsUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Update Jellyfin sync settings."""
    if request.sync_enabled is not None:
        user.jellyfin_sync_enabled = request.sync_enabled

    await db.commit()

    return {
        "status": "success",
        "sync_enabled": user.jellyfin_sync_enabled,
    }


@router.post("/sync", response_model=JellyfinSyncResponse)
async def sync_jellyfin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JellyfinSyncResponse:
    """Trigger manual Jellyfin sync."""
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    if not user.jellyfin_user_id:
        raise HTTPException(status_code=400, detail="Jellyfin user not selected")

    try:
        service = JellyfinSyncService(client)
        results = await service.sync_bidirectional(db, user.id)

        return JellyfinSyncResponse(
            status="success",
            import_result={
                "imported": results["import"].imported,
                "updated": results["import"].updated,
                "skipped": results["import"].skipped,
                "errors": results["import"].errors,
            },
            export_result={
                "exported": results["export"].exported,
                "skipped": results["export"].skipped,
                "errors": results["export"].errors,
            },
        )

    except JellyfinError as e:
        logger.error(f"Jellyfin sync failed for user {user.id}: {e}")
        return JellyfinSyncResponse(
            status="error",
            message=str(e),
        )


@router.post("/sync/import", response_model=JellyfinSyncResponse)
async def import_from_jellyfin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JellyfinSyncResponse:
    """Import media from Jellyfin to Yaad."""
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    if not user.jellyfin_user_id:
        raise HTTPException(status_code=400, detail="Jellyfin user not selected")

    try:
        service = JellyfinSyncService(client)
        result = await service.sync_from_jellyfin(
            db=db,
            user_id=user.id,
            import_new=True,
            update_existing=True,
        )

        return JellyfinSyncResponse(
            status="success",
            import_result={
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": result.errors,
            },
        )

    except JellyfinError as e:
        return JellyfinSyncResponse(status="error", message=str(e))


@router.post("/sync/export", response_model=JellyfinSyncResponse)
async def export_to_jellyfin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JellyfinSyncResponse:
    """Export watch status from Yaad to Jellyfin."""
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    if not user.jellyfin_user_id:
        raise HTTPException(status_code=400, detail="Jellyfin user not selected")

    try:
        service = JellyfinSyncService(client)
        result = await service.sync_to_jellyfin(
            db=db,
            user_id=user.id,
            sync_watched=True,
        )

        return JellyfinSyncResponse(
            status="success",
            export_result={
                "exported": result.exported,
                "skipped": result.skipped,
                "errors": result.errors,
            },
        )

    except JellyfinError as e:
        return JellyfinSyncResponse(status="error", message=str(e))


@router.post("/link")
async def link_existing_media(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Link existing Yaad media to Jellyfin items by TMDB ID.

    Useful for initial setup to connect your existing Yaad library
    with Jellyfin without re-importing everything.
    """
    client = await get_jellyfin_client_for_user(user)
    if not client:
        raise HTTPException(status_code=400, detail="Jellyfin not connected")

    if not user.jellyfin_user_id:
        raise HTTPException(status_code=400, detail="Jellyfin user not selected")

    try:
        service = JellyfinSyncService(client)
        result = await service.link_existing_media(db=db, user_id=user.id)

        return {
            "status": "success",
            "linked": result.updated,
            "skipped": result.skipped,
            "errors": result.errors,
        }

    except JellyfinError as e:
        raise HTTPException(status_code=400, detail=str(e))
