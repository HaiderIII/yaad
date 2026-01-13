"""Jellyfin integration module.

Provides bidirectional sync between Yaad and Jellyfin media server.

Features:
- Import movies and series from Jellyfin
- Sync watch status (played/unplayed)
- Link existing media by TMDB ID
- Automatic periodic sync

Usage:
    from src.services.jellyfin import JellyfinClient, JellyfinSyncService

    # Create client
    client = JellyfinClient(
        server_url="http://jellyfin.local:8096",
        api_key="your-api-key",
        user_id="user-guid"
    )

    # Test connection
    success, message = await client.test_connection()

    # Sync media
    service = JellyfinSyncService(client)
    results = await service.sync_bidirectional(db, user_id)
"""

from src.services.jellyfin.client import (
    JellyfinClient,
    JellyfinError,
    JellyfinAuthError,
    JellyfinConnectionError,
    JellyfinItem,
    JellyfinMediaType,
    JellyfinUser,
    create_jellyfin_client,
)
from src.services.jellyfin.sync import (
    JellyfinSyncService,
    SyncResult,
    get_jellyfin_client_for_user,
    sync_all_jellyfin_users,
    sync_jellyfin_for_user,
)

__all__ = [
    # Client
    "JellyfinClient",
    "JellyfinError",
    "JellyfinAuthError",
    "JellyfinConnectionError",
    "JellyfinItem",
    "JellyfinMediaType",
    "JellyfinUser",
    "create_jellyfin_client",
    # Sync
    "JellyfinSyncService",
    "SyncResult",
    "get_jellyfin_client_for_user",
    "sync_all_jellyfin_users",
    "sync_jellyfin_for_user",
]
