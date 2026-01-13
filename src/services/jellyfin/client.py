"""Jellyfin API client.

Handles communication with Jellyfin server for media synchronization.
Documentation: https://api.jellyfin.org/
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

from src.constants import API_TIMEOUT_EXTERNAL

logger = logging.getLogger(__name__)


class JellyfinMediaType(str, Enum):
    """Jellyfin media types."""

    MOVIE = "Movie"
    SERIES = "Series"
    EPISODE = "Episode"
    BOOK = "Book"
    AUDIO_BOOK = "AudioBook"
    MUSIC = "Audio"


class JellyfinPlaybackStatus(str, Enum):
    """Jellyfin playback status."""

    NONE = "None"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"


@dataclass
class JellyfinItem:
    """Represents a Jellyfin library item."""

    id: str
    name: str
    type: JellyfinMediaType
    year: int | None = None
    overview: str | None = None
    runtime_ticks: int | None = None  # Duration in ticks (1 tick = 100 nanoseconds)
    image_tags: dict[str, str] | None = None
    played: bool = False
    play_count: int = 0
    last_played_date: datetime | None = None
    user_data: dict[str, Any] | None = None
    provider_ids: dict[str, str] | None = None  # TMDB, IMDB, etc.
    series_id: str | None = None  # For episodes
    series_name: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    etag: str | None = None

    @property
    def duration_minutes(self) -> int | None:
        """Convert runtime ticks to minutes."""
        if self.runtime_ticks:
            return self.runtime_ticks // 600_000_000  # 10,000,000 ticks per second
        return None

    @property
    def tmdb_id(self) -> str | None:
        """Get TMDB ID if available."""
        if self.provider_ids:
            return self.provider_ids.get("Tmdb")
        return None

    @property
    def imdb_id(self) -> str | None:
        """Get IMDB ID if available."""
        if self.provider_ids:
            return self.provider_ids.get("Imdb")
        return None


@dataclass
class JellyfinUser:
    """Represents a Jellyfin user."""

    id: str
    name: str
    server_id: str
    has_password: bool = True


class JellyfinError(Exception):
    """Base exception for Jellyfin errors."""

    pass


class JellyfinAuthError(JellyfinError):
    """Authentication error."""

    pass


class JellyfinConnectionError(JellyfinError):
    """Connection error."""

    pass


class JellyfinClient:
    """Client for Jellyfin API.

    Usage:
        client = JellyfinClient(
            server_url="http://jellyfin.local:8096",
            api_key="your-api-key",
            user_id="user-guid"
        )

        # Get all movies
        movies = await client.get_items(media_type=JellyfinMediaType.MOVIE)

        # Mark as watched
        await client.mark_played(item_id="movie-guid")

        # Update playback progress
        await client.update_progress(item_id="movie-guid", position_ticks=123456789)
    """

    def __init__(
        self,
        server_url: str,
        api_key: str,
        user_id: str | None = None,
        device_id: str = "yaad-sync",
        device_name: str = "Yaad",
        client_name: str = "Yaad",
        client_version: str = "0.1.0",
    ):
        """Initialize Jellyfin client.

        Args:
            server_url: Jellyfin server URL (e.g., http://localhost:8096)
            api_key: API key generated in Jellyfin dashboard
            user_id: User GUID for user-specific operations
            device_id: Unique device identifier
            device_name: Device name shown in Jellyfin
            client_name: Client application name
            client_version: Client version
        """
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.device_id = device_id

        # Build authorization header
        self._auth_header = (
            f'MediaBrowser Client="{client_name}", '
            f'Device="{device_name}", '
            f'DeviceId="{device_id}", '
            f'Version="{client_version}", '
            f'Token="{api_key}"'
        )

    def _get_headers(self) -> dict[str, str]:
        """Get request headers."""
        return {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        """Make API request.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            params: Query parameters
            json: JSON body

        Returns:
            Response data or None for 204 responses

        Raises:
            JellyfinAuthError: On 401/403 errors
            JellyfinConnectionError: On connection errors
            JellyfinError: On other errors
        """
        url = f"{self.server_url}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_EXTERNAL) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self._get_headers(),
                    params=params,
                    json=json,
                )

                if response.status_code == 204:
                    return None

                if response.status_code == 401:
                    raise JellyfinAuthError("Invalid API key")

                if response.status_code == 403:
                    raise JellyfinAuthError("Access denied")

                if response.status_code >= 400:
                    raise JellyfinError(
                        f"API error {response.status_code}: {response.text}"
                    )

                return response.json()

        except httpx.ConnectError as e:
            raise JellyfinConnectionError(f"Cannot connect to Jellyfin: {e}")
        except httpx.TimeoutException as e:
            raise JellyfinConnectionError(f"Connection timeout: {e}")

    # ==================== Server Info ====================

    async def get_server_info(self) -> dict[str, Any]:
        """Get server information.

        Returns:
            Server info including version, name, etc.
        """
        result = await self._request("GET", "/System/Info/Public")
        return result or {}

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection to Jellyfin server.

        Returns:
            Tuple of (success, message)
        """
        try:
            info = await self.get_server_info()
            server_name = info.get("ServerName", "Unknown")
            version = info.get("Version", "Unknown")
            return True, f"Connected to {server_name} (v{version})"
        except JellyfinAuthError as e:
            return False, f"Authentication failed: {e}"
        except JellyfinConnectionError as e:
            return False, f"Connection failed: {e}"
        except Exception as e:
            return False, f"Error: {e}"

    # ==================== Users ====================

    async def get_users(self) -> list[JellyfinUser]:
        """Get list of users.

        Returns:
            List of Jellyfin users
        """
        result = await self._request("GET", "/Users")
        if not result:
            return []

        return [
            JellyfinUser(
                id=u["Id"],
                name=u["Name"],
                server_id=u.get("ServerId", ""),
                has_password=u.get("HasPassword", True),
            )
            for u in result
        ]

    async def get_current_user(self) -> JellyfinUser | None:
        """Get current user info.

        Returns:
            Current user or None
        """
        if not self.user_id:
            return None

        result = await self._request("GET", f"/Users/{self.user_id}")
        if not result:
            return None

        return JellyfinUser(
            id=result["Id"],
            name=result["Name"],
            server_id=result.get("ServerId", ""),
            has_password=result.get("HasPassword", True),
        )

    # ==================== Library Items ====================

    async def get_items(
        self,
        media_type: JellyfinMediaType | None = None,
        parent_id: str | None = None,
        search_term: str | None = None,
        is_played: bool | None = None,
        limit: int = 100,
        start_index: int = 0,
        sort_by: str = "SortName",
        sort_order: str = "Ascending",
        include_item_types: list[str] | None = None,
        recursive: bool = True,
    ) -> tuple[list[JellyfinItem], int]:
        """Get library items.

        Args:
            media_type: Filter by media type
            parent_id: Parent folder ID
            search_term: Search query
            is_played: Filter by played status
            limit: Max items to return
            start_index: Pagination offset
            sort_by: Sort field
            sort_order: Ascending or Descending
            include_item_types: List of types to include
            recursive: Search recursively

        Returns:
            Tuple of (items, total_count)
        """
        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        params: dict[str, Any] = {
            "Recursive": recursive,
            "Limit": limit,
            "StartIndex": start_index,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "Fields": "Overview,ProviderIds,UserData,DateCreated",
        }

        if media_type:
            params["IncludeItemTypes"] = media_type.value
        elif include_item_types:
            params["IncludeItemTypes"] = ",".join(include_item_types)

        if parent_id:
            params["ParentId"] = parent_id

        if search_term:
            params["SearchTerm"] = search_term

        if is_played is not None:
            params["IsPlayed"] = is_played

        result = await self._request(
            "GET", f"/Users/{self.user_id}/Items", params=params
        )

        if not result:
            return [], 0

        items = [self._parse_item(item) for item in result.get("Items", [])]
        total = result.get("TotalRecordCount", len(items))

        return items, total

    async def get_item(self, item_id: str) -> JellyfinItem | None:
        """Get a single item by ID.

        Args:
            item_id: Item GUID

        Returns:
            Item or None if not found
        """
        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        try:
            result = await self._request(
                "GET",
                f"/Users/{self.user_id}/Items/{item_id}",
            )
            if result:
                return self._parse_item(result)
        except JellyfinError:
            pass

        return None

    async def get_item_by_provider_id(
        self,
        provider: str,
        provider_id: str,
        media_type: JellyfinMediaType | None = None,
    ) -> JellyfinItem | None:
        """Find item by external provider ID (TMDB, IMDB, etc.).

        Args:
            provider: Provider name (Tmdb, Imdb, etc.)
            provider_id: External ID
            media_type: Optional media type filter

        Returns:
            Item or None if not found
        """
        params: dict[str, Any] = {
            f"Any{provider}Id": provider_id,
            "Recursive": True,
            "Limit": 1,
            "Fields": "Overview,ProviderIds,UserData,DateCreated",
        }

        if media_type:
            params["IncludeItemTypes"] = media_type.value

        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        result = await self._request(
            "GET", f"/Users/{self.user_id}/Items", params=params
        )

        if result and result.get("Items"):
            return self._parse_item(result["Items"][0])

        return None

    def _parse_item(self, data: dict[str, Any]) -> JellyfinItem:
        """Parse API response into JellyfinItem."""
        user_data = data.get("UserData", {})

        last_played = None
        if user_data.get("LastPlayedDate"):
            try:
                last_played = datetime.fromisoformat(
                    user_data["LastPlayedDate"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        return JellyfinItem(
            id=data["Id"],
            name=data.get("Name", ""),
            type=JellyfinMediaType(data.get("Type", "Movie")),
            year=data.get("ProductionYear"),
            overview=data.get("Overview"),
            runtime_ticks=data.get("RunTimeTicks"),
            image_tags=data.get("ImageTags"),
            played=user_data.get("Played", False),
            play_count=user_data.get("PlayCount", 0),
            last_played_date=last_played,
            user_data=user_data,
            provider_ids=data.get("ProviderIds"),
            series_id=data.get("SeriesId"),
            series_name=data.get("SeriesName"),
            season_number=data.get("ParentIndexNumber"),
            episode_number=data.get("IndexNumber"),
            etag=data.get("Etag"),
        )

    # ==================== Playback Status ====================

    async def mark_played(self, item_id: str) -> bool:
        """Mark item as played.

        Args:
            item_id: Item GUID

        Returns:
            True if successful
        """
        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        await self._request(
            "POST", f"/Users/{self.user_id}/PlayedItems/{item_id}"
        )
        logger.info(f"Marked item {item_id} as played")
        return True

    async def mark_unplayed(self, item_id: str) -> bool:
        """Mark item as unplayed.

        Args:
            item_id: Item GUID

        Returns:
            True if successful
        """
        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        await self._request(
            "DELETE", f"/Users/{self.user_id}/PlayedItems/{item_id}"
        )
        logger.info(f"Marked item {item_id} as unplayed")
        return True

    async def update_progress(
        self,
        item_id: str,
        position_ticks: int,
        is_paused: bool = False,
    ) -> bool:
        """Update playback progress.

        Args:
            item_id: Item GUID
            position_ticks: Current position in ticks
            is_paused: Whether playback is paused

        Returns:
            True if successful
        """
        if not self.user_id:
            raise JellyfinError("User ID required for this operation")

        await self._request(
            "POST",
            f"/Users/{self.user_id}/PlayingItems/{item_id}/Progress",
            params={
                "PositionTicks": position_ticks,
                "IsPaused": is_paused,
            },
        )
        return True

    # ==================== Images ====================

    def get_image_url(
        self,
        item_id: str,
        image_type: str = "Primary",
        max_width: int = 400,
    ) -> str:
        """Get URL for item image.

        Args:
            item_id: Item GUID
            image_type: Primary, Backdrop, Banner, etc.
            max_width: Maximum image width

        Returns:
            Image URL
        """
        return (
            f"{self.server_url}/Items/{item_id}/Images/{image_type}"
            f"?maxWidth={max_width}&quality=90"
        )


# Factory function for creating client from user settings
def create_jellyfin_client(
    server_url: str,
    api_key: str,
    user_id: str | None = None,
) -> JellyfinClient:
    """Create a Jellyfin client instance.

    Args:
        server_url: Jellyfin server URL
        api_key: API key
        user_id: Optional user ID

    Returns:
        Configured JellyfinClient
    """
    return JellyfinClient(
        server_url=server_url,
        api_key=api_key,
        user_id=user_id,
    )
