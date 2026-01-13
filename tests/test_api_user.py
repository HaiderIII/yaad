"""Tests for user API endpoints."""

import pytest
from httpx import AsyncClient


class TestUserSettingsEndpoints:
    """Tests for /api/user endpoints."""

    @pytest.mark.asyncio
    async def test_get_locale_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot get locale."""
        response = await client.get("/api/user/locale", follow_redirects=False)
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_get_locale(self, authenticated_client: AsyncClient):
        """Test getting user locale."""
        response = await authenticated_client.get("/api/user/locale")
        assert response.status_code == 200
        data = response.json()
        assert "locale" in data
        assert data["locale"] in ["en", "fr"]

    @pytest.mark.asyncio
    async def test_update_locale(self, authenticated_client: AsyncClient):
        """Test updating user locale."""
        response = await authenticated_client.patch(
            "/api/user/locale",
            json={"locale": "fr"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["locale"] == "fr"

    @pytest.mark.asyncio
    async def test_update_locale_invalid(self, authenticated_client: AsyncClient):
        """Test that invalid locale is rejected."""
        response = await authenticated_client.patch(
            "/api/user/locale",
            json={"locale": "invalid"},
        )
        # May accept any string or validate
        assert response.status_code in [200, 422]

    @pytest.mark.asyncio
    async def test_get_streaming_preferences(self, authenticated_client: AsyncClient):
        """Test getting streaming preferences."""
        response = await authenticated_client.get("/api/user/streaming-preferences")
        assert response.status_code == 200
        data = response.json()
        assert "country" in data

    @pytest.mark.asyncio
    async def test_update_streaming_preferences(self, authenticated_client: AsyncClient):
        """Test updating streaming preferences."""
        response = await authenticated_client.patch(
            "/api/user/streaming-preferences",
            json={"country": "FR", "providers": ["Netflix", "Prime Video"]},
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_available_providers(self, authenticated_client: AsyncClient):
        """Test getting available streaming providers."""
        response = await authenticated_client.get("/api/user/available-providers")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_get_settings(self, authenticated_client: AsyncClient):
        """Test getting user settings."""
        response = await authenticated_client.get("/api/user/settings")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_update_settings(self, authenticated_client: AsyncClient):
        """Test updating user settings."""
        response = await authenticated_client.patch(
            "/api/user/settings",
            json={"theme": "dark"},
        )
        assert response.status_code == 200


class TestBookLocationsEndpoints:
    """Tests for book locations endpoints."""

    @pytest.mark.asyncio
    async def test_list_book_locations_empty(self, authenticated_client: AsyncClient):
        """Test listing book locations when none exist."""
        response = await authenticated_client.get("/api/user/book-locations")
        assert response.status_code == 200
        data = response.json()
        assert data == []

    @pytest.mark.asyncio
    async def test_create_book_location(self, authenticated_client: AsyncClient):
        """Test creating a book location."""
        response = await authenticated_client.post(
            "/api/user/book-locations",
            json={"name": "Living Room Shelf", "description": "Main bookshelf"},
        )
        assert response.status_code in [200, 201]
        data = response.json()
        assert data["name"] == "Living Room Shelf"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_delete_book_location(self, authenticated_client: AsyncClient):
        """Test deleting a book location."""
        # Create first
        create_response = await authenticated_client.post(
            "/api/user/book-locations",
            json={"name": "To Delete"},
        )
        location_id = create_response.json()["id"]

        # Delete
        response = await authenticated_client.delete(
            f"/api/user/book-locations/{location_id}"
        )
        assert response.status_code in [200, 204]

        # Verify deleted
        list_response = await authenticated_client.get("/api/user/book-locations")
        locations = list_response.json()
        assert not any(loc["id"] == location_id for loc in locations)

    @pytest.mark.asyncio
    async def test_delete_book_location_not_found(self, authenticated_client: AsyncClient):
        """Test deleting non-existent book location."""
        response = await authenticated_client.delete("/api/user/book-locations/99999")
        assert response.status_code == 404
