"""Tests for Jellyfin API endpoints."""

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


class TestJellyfinEndpoints:
    """Tests for /api/jellyfin endpoints."""

    @pytest.mark.asyncio
    async def test_status_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot get Jellyfin status."""
        response = await client.get("/api/jellyfin/status", follow_redirects=False)
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_status_not_connected(self, authenticated_client: AsyncClient):
        """Test status when Jellyfin is not connected."""
        response = await authenticated_client.get("/api/jellyfin/status")
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False

    @pytest.mark.asyncio
    async def test_connect_missing_fields(self, authenticated_client: AsyncClient):
        """Test connect with missing required fields."""
        response = await authenticated_client.post(
            "/api/jellyfin/connect",
            json={"server_url": "http://localhost:8096"},
            # Missing api_key
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_connect_invalid_url(self, authenticated_client: AsyncClient):
        """Test connect with invalid server URL."""
        with patch("src.services.jellyfin.client.JellyfinClient.test_connection") as mock:
            mock.return_value = (False, "Connection refused")

            response = await authenticated_client.post(
                "/api/jellyfin/connect",
                json={
                    "server_url": "http://invalid-server:8096",
                    "api_key": "test-api-key",
                },
            )
            # Should fail with connection error
            assert response.status_code in [400, 502]

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self, authenticated_client: AsyncClient):
        """Test disconnect when not connected."""
        response = await authenticated_client.delete("/api/jellyfin/disconnect")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "disconnected"

    @pytest.mark.asyncio
    async def test_users_not_connected(self, authenticated_client: AsyncClient):
        """Test getting users when not connected."""
        response = await authenticated_client.get("/api/jellyfin/users")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_select_user_not_connected(self, authenticated_client: AsyncClient):
        """Test selecting user when not connected."""
        response = await authenticated_client.post(
            "/api/jellyfin/users/select",
            json={"user_id": "some-user-id"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_update_settings(self, authenticated_client: AsyncClient):
        """Test updating Jellyfin settings."""
        response = await authenticated_client.patch(
            "/api/jellyfin/settings",
            json={"sync_enabled": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["sync_enabled"] is True

    @pytest.mark.asyncio
    async def test_sync_not_connected(self, authenticated_client: AsyncClient):
        """Test sync when not connected."""
        response = await authenticated_client.post("/api/jellyfin/sync")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_import_not_connected(self, authenticated_client: AsyncClient):
        """Test import when not connected."""
        response = await authenticated_client.post("/api/jellyfin/sync/import")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_export_not_connected(self, authenticated_client: AsyncClient):
        """Test export when not connected."""
        response = await authenticated_client.post("/api/jellyfin/sync/export")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_link_not_connected(self, authenticated_client: AsyncClient):
        """Test link when not connected."""
        response = await authenticated_client.post("/api/jellyfin/link")
        assert response.status_code == 400


class TestJellyfinValidation:
    """Tests for Jellyfin request validation."""

    @pytest.mark.asyncio
    async def test_connect_empty_url(self, authenticated_client: AsyncClient):
        """Test connect with empty server URL."""
        response = await authenticated_client.post(
            "/api/jellyfin/connect",
            json={"server_url": "", "api_key": "test"},
        )
        # Empty URL may be allowed but fail on connection, or rejected
        assert response.status_code in [400, 422, 502]

    @pytest.mark.asyncio
    async def test_connect_empty_api_key(self, authenticated_client: AsyncClient):
        """Test connect with empty API key."""
        response = await authenticated_client.post(
            "/api/jellyfin/connect",
            json={"server_url": "http://localhost:8096", "api_key": ""},
        )
        # Empty key may be allowed but fail on connection, or rejected
        assert response.status_code in [400, 422, 502]

    @pytest.mark.asyncio
    async def test_select_user_empty_id(self, authenticated_client: AsyncClient):
        """Test selecting user with empty ID."""
        response = await authenticated_client.post(
            "/api/jellyfin/users/select",
            json={"user_id": ""},
        )
        assert response.status_code in [400, 422]
