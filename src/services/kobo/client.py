"""Kobo API client for library sync and reading progress.

Based on the Kobo Store API reverse engineering from:
https://github.com/subdavis/kobo-book-downloader
"""

import base64
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from src.utils.http_client import get_general_client

logger = logging.getLogger(__name__)

# Kobo API endpoints
KOBO_API_BASE = "https://storeapi.kobo.com"
KOBO_AUTH_ACTIVATE = "https://auth.kobobooks.com/ActivateOnWeb"
KOBO_AUTH_DEVICE = f"{KOBO_API_BASE}/v1/auth/device"
KOBO_AUTH_REFRESH = f"{KOBO_API_BASE}/v1/auth/refresh"
KOBO_INIT = f"{KOBO_API_BASE}/v1/initialization"
KOBO_ACTIVATE_URL = "https://www.kobo.com/activate"

# Device simulation constants (mimics Android Kobo app)
KOBO_AFFILIATE = "Kobo"
KOBO_APP_VERSION = "4.37.21586"
KOBO_PLATFORM_ID = "00000000-0000-0000-0000-000000000388"  # Android


@dataclass
class KoboCredentials:
    """Kobo device credentials."""

    device_id: str
    user_key: str
    access_token: str | None = None
    refresh_token: str | None = None


@dataclass
class KoboBook:
    """Simplified Kobo book info."""

    id: str
    title: str
    author: str | None
    isbn: str | None
    cover_url: str | None
    percent_read: float  # 0-100
    last_read: datetime | None
    is_finished: bool
    is_audiobook: bool = False
    description: str | None = None
    publisher: str | None = None


@dataclass
class ActivationInfo:
    """Activation info for user."""

    activation_url: str
    user_code: str
    device_id: str
    polling_url: str


class KoboClient:
    """Client for Kobo API interactions."""

    def __init__(self) -> None:
        self._endpoints: dict[str, str] = {}

    def _generate_device_id(self) -> str:
        """Generate a random device ID."""
        return str(uuid.uuid4())

    def _get_device_headers(self, access_token: str | None = None) -> dict[str, str]:
        """Get headers for API requests."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-kobo-affiliate": KOBO_AFFILIATE,
            "x-kobo-appversion": KOBO_APP_VERSION,
            "x-kobo-platformid": KOBO_PLATFORM_ID,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    async def start_activation(self) -> ActivationInfo | None:
        """Start the activation process via Kobo's web activation.

        Returns:
            ActivationInfo with URL for user to visit and code to enter (6 digits).
        """
        device_id = self._generate_device_id()

        params = {
            "PlatformId": KOBO_PLATFORM_ID,
            "DeviceId": device_id,
            "AppVersion": KOBO_APP_VERSION,
        }

        client = get_general_client()
        try:
            response = await client.get(
                KOBO_AUTH_ACTIVATE,
                params=params,
                follow_redirects=True,
            )

            if response.status_code != 200:
                logger.error(f"Kobo activation start failed: {response.status_code}")
                return None

            html = response.text

            # Extract 6-digit activation code from QR code generator URL
            # Pattern: qrcodegenerator/generate...%26code%3D123456
            code_match = re.search(r"qrcodegenerator/generate.+?%26code%3D(\d+)", html)
            if not code_match:
                # Try alternative patterns
                code_match = re.search(r'code["\s:=]+["\']?(\d{6})["\']?', html, re.IGNORECASE)

            if not code_match:
                # Try to find any 6-digit number that looks like a code
                code_match = re.search(r'\b(\d{6})\b', html)

            if not code_match:
                logger.error("Could not extract activation code from Kobo response")
                logger.debug(f"Response HTML: {html[:1000]}")
                return None

            user_code = code_match.group(1).strip()

            # Extract polling URL from data-poll-endpoint attribute
            poll_match = re.search(r'data-poll-endpoint="([^"]+)"', html)
            if not poll_match:
                # Try alternative patterns
                poll_match = re.search(r'data-poll-url="([^"]*)"', html)
            if not poll_match:
                poll_match = re.search(r'pollUrl["\s:=]+["\']([^"\']+)["\']', html)

            polling_url = ""
            if poll_match:
                endpoint = poll_match.group(1)
                # Make sure it's a full URL
                if endpoint.startswith("/"):
                    polling_url = f"https://auth.kobobooks.com{endpoint}"
                elif not endpoint.startswith("http"):
                    polling_url = f"https://auth.kobobooks.com/{endpoint}"
                else:
                    polling_url = endpoint

            return ActivationInfo(
                activation_url=KOBO_ACTIVATE_URL,
                user_code=user_code,
                device_id=device_id,
                polling_url=polling_url,
            )

        except httpx.RequestError as e:
            logger.error(f"Kobo activation request failed: {e}")
            return None

    async def check_activation(self, device_id: str, polling_url: str) -> dict | None:
        """Check if activation has been completed.

        Returns:
            Dict with user_key and email if complete, None if still pending or failed.
        """
        client = get_general_client()
        try:
            # If polling_url doesn't have device ID, add it
            if "DeviceId" not in polling_url:
                separator = "&" if "?" in polling_url else "?"
                polling_url = f"{polling_url}{separator}DeviceId={device_id}"

            # Use POST as per Kobo's API
            response = await client.post(polling_url, follow_redirects=False)

            # Check for redirect (indicates completion)
            if response.status_code in (301, 302, 303, 307, 308):
                redirect_url = response.headers.get("Location", "")
                return self._extract_user_key_from_url(redirect_url)

            # Check response body for status
            if response.status_code == 200:
                # Try JSON first
                try:
                    data = response.json()
                    if data.get("Status") == "Complete":
                        # Extract from RedirectUrl if present
                        redirect_url = data.get("RedirectUrl", "")
                        if redirect_url:
                            result = self._extract_user_key_from_url(redirect_url)
                            if result:
                                return result
                        # Fallback to direct fields
                        return {
                            "user_key": data.get("UserKey"),
                            "user_id": data.get("UserId"),
                            "email": data.get("Email"),
                            "complete": True,
                        }
                except Exception:
                    # Not JSON, check HTML for userKey
                    html = response.text

                    # Look for userKey in various patterns
                    user_key_match = re.search(r'userKey["\s:=]+["\'"]?([a-zA-Z0-9\-]+)["\']?', html, re.IGNORECASE)
                    if user_key_match:
                        return {
                            "user_key": user_key_match.group(1),
                            "complete": True,
                        }

                    # Check if success page
                    if "activée" in html.lower() or "activated" in html.lower() or "success" in html.lower():
                        # Try to extract from URL in page
                        url_match = re.search(r'href=["\']([^"\']*userKey[^"\']*)["\']', html)
                        if url_match:
                            return self._extract_user_key_from_url(url_match.group(1))

            return None  # Still pending

        except httpx.RequestError as e:
            logger.error(f"Kobo activation check failed: {e}")
            return None

    def _extract_user_key_from_url(self, url: str) -> dict | None:
        """Extract userKey and other params from URL."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        # Try different parameter name variations
        user_key = (
            params.get("userKey", [None])[0]
            or params.get("UserKey", [None])[0]
            or params.get("user_key", [None])[0]
        )

        if user_key:
            return {
                "user_key": user_key,
                "user_id": params.get("userId", params.get("UserId", [None]))[0],
                "email": params.get("email", params.get("Email", [None]))[0],
                "complete": True,
            }
        return None

    async def authenticate_device(
        self, device_id: str, user_key: str
    ) -> KoboCredentials | None:
        """Authenticate the device with Kobo servers.

        Args:
            device_id: The device ID
            user_key: The user key from activation

        Returns:
            KoboCredentials if successful, None otherwise.
        """
        # ClientKey must be base64-encoded PlatformId (per Kobo API requirements)
        client_key = base64.b64encode(KOBO_PLATFORM_ID.encode()).decode()

        auth_data = {
            "AffiliateName": KOBO_AFFILIATE,
            "AppVersion": KOBO_APP_VERSION,
            "ClientKey": client_key,
            "DeviceId": device_id,
            "PlatformId": KOBO_PLATFORM_ID,
            "UserKey": user_key,
        }

        client = get_general_client()
        try:
            response = await client.post(
                KOBO_AUTH_DEVICE,
                json=auth_data,
                headers=self._get_device_headers(),
            )

            if response.status_code != 200:
                logger.error(f"Device auth failed: {response.status_code} - {response.text[:200]}")
                return None

            data = response.json()

            return KoboCredentials(
                device_id=device_id,
                user_key=user_key,
                access_token=data.get("AccessToken"),
                refresh_token=data.get("RefreshToken"),
            )

        except httpx.RequestError as e:
            logger.error(f"Kobo device auth request failed: {e}")
            return None

    async def refresh_token(self, credentials: KoboCredentials) -> KoboCredentials | None:
        """Refresh the access token."""
        if not credentials.refresh_token:
            return None

        # ClientKey must be base64-encoded PlatformId (per Kobo API requirements)
        client_key = base64.b64encode(KOBO_PLATFORM_ID.encode()).decode()

        refresh_data = {
            "AppVersion": KOBO_APP_VERSION,
            "ClientKey": client_key,
            "DeviceId": credentials.device_id,
            "PlatformId": KOBO_PLATFORM_ID,
            "RefreshToken": credentials.refresh_token,
        }

        client = get_general_client()
        try:
            response = await client.post(
                KOBO_AUTH_REFRESH,
                json=refresh_data,
                headers=self._get_device_headers(),
            )

            if response.status_code != 200:
                logger.warning(f"Kobo token refresh failed: {response.status_code}")
                return None

            data = response.json()

            return KoboCredentials(
                device_id=credentials.device_id,
                user_key=credentials.user_key,
                access_token=data.get("AccessToken"),
                refresh_token=data.get("RefreshToken"),
            )

        except httpx.RequestError as e:
            logger.error(f"Kobo refresh request failed: {e}")
            return None

    async def _load_endpoints(self, access_token: str) -> bool:
        """Load API endpoints from initialization."""
        client = get_general_client()
        try:
            response = await client.get(
                KOBO_INIT,
                headers=self._get_device_headers(access_token),
            )

            if response.status_code != 200:
                logger.error(f"Init failed: {response.status_code}")
                return False

            data = response.json()
            resources = data.get("Resources", {})

            self._endpoints = {
                "library_sync": resources.get("library_sync"),
                "library_items": resources.get("library_items"),
                "user_profile": resources.get("user_profile"),
            }

            return True

        except httpx.RequestError as e:
            logger.error(f"Failed to load Kobo endpoints: {e}")
            return False

    async def get_library(self, credentials: KoboCredentials) -> list[KoboBook]:
        """Get user's Kobo library with reading progress."""
        if not credentials.access_token:
            return []

        if not self._endpoints:
            if not await self._load_endpoints(credentials.access_token):
                return []

        library_url = self._endpoints.get("library_sync")
        if not library_url:
            logger.error("No library_sync endpoint found")
            return []

        books: list[KoboBook] = []
        sync_token: str | None = None

        client = get_general_client()
        while True:
            headers = self._get_device_headers(credentials.access_token)
            if sync_token:
                headers["x-kobo-synctoken"] = sync_token

            try:
                response = await client.get(library_url, headers=headers)

                if response.status_code != 200:
                    logger.warning(f"Library sync failed: {response.status_code}")
                    break

                sync_token = response.headers.get("x-kobo-synctoken")
                sync_continuation = response.headers.get("x-kobo-sync")

                data = response.json()

                for item in data:
                    book = self._parse_library_item(item)
                    if book:
                        books.append(book)

                if sync_continuation != "continue":
                    break

            except httpx.RequestError as e:
                logger.error(f"Library request failed: {e}")
                break

        return books

    def _parse_library_item(self, item: dict[str, Any]) -> KoboBook | None:
        """Parse a library item into KoboBook."""
        try:
            # New API format: data is inside NewEntitlement
            new_entitlement = item.get("NewEntitlement", {})
            if not new_entitlement:
                return None

            # Check if it's a book or audiobook entitlement
            book_entitlement = new_entitlement.get("BookEntitlement", {})
            audiobook_entitlement = new_entitlement.get("AudiobookEntitlement", {})

            # Use whichever entitlement exists
            entitlement = book_entitlement or audiobook_entitlement
            if not entitlement:
                return None

            is_audiobook = bool(audiobook_entitlement)

            book_meta = new_entitlement.get("BookMetadata", {})
            if not book_meta:
                # No metadata means we can't get title/author/etc
                return None
            reading_state = new_entitlement.get("ReadingState", {})
            status_info = reading_state.get("StatusInfo", {})

            location = reading_state.get("CurrentBookmark", {})
            percent = location.get("ProgressPercent", 0.0)

            last_read = None
            last_modified = reading_state.get("LastModified")
            if last_modified:
                try:
                    last_read = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            is_finished = status_info.get("Status") == "Finished"

            # Get author from ContributorRoles
            contributors = book_meta.get("ContributorRoles", [])
            author = None
            for contrib in contributors:
                if contrib.get("Role") == "Author":
                    author = contrib.get("Name")
                    break

            # Get title
            title = book_meta.get("Title", "Unknown")

            # Get ISBN (field name is "Isbn" not "ISBN")
            isbn = book_meta.get("Isbn") or book_meta.get("ISBN")

            # Get description and publisher
            description = book_meta.get("Description")
            publisher = book_meta.get("Publisher", {}).get("Name") if isinstance(book_meta.get("Publisher"), dict) else book_meta.get("Publisher")

            # Build cover URL from CoverImageId
            cover_image_id = book_meta.get("CoverImageId")
            cover_url = None
            if cover_image_id:
                # Kobo cover URL format
                cover_url = f"https://cdn.kobo.com/book-images/{cover_image_id}"

            return KoboBook(
                id=entitlement.get("RevisionId") or book_meta.get("RevisionId", ""),
                title=title,
                author=author,
                isbn=isbn,
                cover_url=cover_url,
                percent_read=percent if percent > 1 else percent * 100,
                last_read=last_read,
                is_finished=is_finished,
                is_audiobook=is_audiobook,
                description=description,
                publisher=publisher,
            )

        except (KeyError, TypeError) as e:
            logger.warning(f"Failed to parse library item: {e}")
            return None

    async def validate_credentials(self, credentials: KoboCredentials) -> bool:
        """Check if credentials are still valid."""
        if not credentials.access_token:
            return False

        client = get_general_client()
        try:
            response = await client.get(
                KOBO_INIT,
                headers=self._get_device_headers(credentials.access_token),
            )
            return response.status_code == 200

        except httpx.RequestError:
            return False


# Singleton instance
kobo_client = KoboClient()
