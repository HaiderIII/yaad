"""Tests for media API endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import User


class TestMediaEndpoints:
    """Tests for /api/media endpoints."""

    @pytest.mark.asyncio
    async def test_create_media_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot create media."""
        response = await client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Test Movie",
                "status": "to_consume",
            },
            follow_redirects=False,
        )
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_create_film(self, authenticated_client: AsyncClient):
        """Test creating a film via API."""
        response = await authenticated_client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Inception",
                "year": 2010,
                "status": "to_consume",
                "duration_minutes": 148,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Inception"
        assert data["year"] == 2010
        assert data["type"] == "film"

    @pytest.mark.asyncio
    async def test_create_book(self, authenticated_client: AsyncClient):
        """Test creating a book via API."""
        # Note: authors are passed as query params, not in body
        response = await authenticated_client.post(
            "/api/media?authors=George%20Orwell",
            json={
                "type": "book",
                "title": "1984",
                "year": 1949,
                "status": "finished",
                "page_count": 328,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "1984"
        assert data["type"] == "book"

    @pytest.mark.asyncio
    async def test_create_media_with_genres(self, authenticated_client: AsyncClient):
        """Test creating media with genres."""
        # Note: genres are passed as query params, not in body
        response = await authenticated_client.post(
            "/api/media?genres=Science%20Fiction&genres=Action",
            json={
                "type": "film",
                "title": "The Matrix",
                "year": 1999,
                "status": "finished",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["genres"]) == 2

    @pytest.mark.asyncio
    async def test_list_media_empty(self, authenticated_client: AsyncClient):
        """Test listing media when none exist."""
        response = await authenticated_client.get("/api/media")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_media_with_items(self, authenticated_client: AsyncClient):
        """Test listing media with items."""
        # Create some media first
        for i in range(3):
            await authenticated_client.post(
                "/api/media",
                json={
                    "type": "film",
                    "title": f"Movie {i}",
                    "status": "to_consume",
                },
            )

        response = await authenticated_client.get("/api/media")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 3
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_list_media_filter_by_type(self, authenticated_client: AsyncClient):
        """Test filtering media by type."""
        # Create film and book
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Film", "status": "to_consume"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "book", "title": "Book", "status": "to_consume"},
        )

        response = await authenticated_client.get("/api/media?type=film")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["type"] == "film"

    @pytest.mark.asyncio
    async def test_list_media_filter_by_status(self, authenticated_client: AsyncClient):
        """Test filtering media by status."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "To Watch", "status": "to_consume"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Watched", "status": "finished"},
        )

        response = await authenticated_client.get("/api/media?status=finished")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == "Watched"

    @pytest.mark.asyncio
    async def test_list_media_pagination(self, authenticated_client: AsyncClient):
        """Test pagination."""
        for i in range(15):
            await authenticated_client.post(
                "/api/media",
                json={"type": "film", "title": f"Movie {i:02d}", "status": "to_consume"},
            )

        # First page
        response = await authenticated_client.get("/api/media?page=1&page_size=10")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 10
        assert data["total"] == 15

        # Second page
        response = await authenticated_client.get("/api/media?page=2&page_size=10")
        data = response.json()
        assert len(data["items"]) == 5

    @pytest.mark.asyncio
    async def test_list_media_search(self, authenticated_client: AsyncClient):
        """Test search functionality."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "The Matrix", "status": "to_consume"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Inception", "status": "to_consume"},
        )

        response = await authenticated_client.get("/api/media?search=matrix")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert "Matrix" in data["items"][0]["title"]

    @pytest.mark.asyncio
    async def test_get_media(self, authenticated_client: AsyncClient):
        """Test getting a single media item."""
        # Create media
        create_response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Test Film", "status": "to_consume"},
        )
        media_id = create_response.json()["id"]

        # Get it
        response = await authenticated_client.get(f"/api/media/{media_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == media_id
        assert data["title"] == "Test Film"

    @pytest.mark.asyncio
    async def test_get_media_not_found(self, authenticated_client: AsyncClient):
        """Test getting non-existent media."""
        response = await authenticated_client.get("/api/media/99999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_media(self, authenticated_client: AsyncClient):
        """Test updating media."""
        # Create media
        create_response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Original Title", "status": "to_consume"},
        )
        media_id = create_response.json()["id"]

        # Update it
        response = await authenticated_client.patch(
            f"/api/media/{media_id}",
            json={"title": "Updated Title", "status": "finished", "rating": 4.5},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["status"] == "finished"
        assert data["rating"] == 4.5

    @pytest.mark.asyncio
    async def test_update_media_not_found(self, authenticated_client: AsyncClient):
        """Test updating non-existent media."""
        response = await authenticated_client.patch(
            "/api/media/99999",
            json={"title": "New Title"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_media(self, authenticated_client: AsyncClient):
        """Test deleting media."""
        # Create media
        create_response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "To Delete", "status": "to_consume"},
        )
        media_id = create_response.json()["id"]

        # Delete it
        response = await authenticated_client.delete(f"/api/media/{media_id}")
        assert response.status_code in [200, 204]

        # Verify it's gone
        get_response = await authenticated_client.get(f"/api/media/{media_id}")
        assert get_response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_media_not_found(self, authenticated_client: AsyncClient):
        """Test deleting non-existent media."""
        response = await authenticated_client.delete("/api/media/99999")
        assert response.status_code == 404


class TestMediaValidation:
    """Tests for media validation."""

    @pytest.mark.asyncio
    async def test_create_media_missing_type(self, authenticated_client: AsyncClient):
        """Test that type is required."""
        response = await authenticated_client.post(
            "/api/media",
            json={"title": "Test", "status": "to_consume"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_media_missing_title(self, authenticated_client: AsyncClient):
        """Test that title is required."""
        response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "status": "to_consume"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_media_invalid_type(self, authenticated_client: AsyncClient):
        """Test that invalid type is rejected."""
        response = await authenticated_client.post(
            "/api/media",
            json={"type": "invalid", "title": "Test", "status": "to_consume"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_media_invalid_status(self, authenticated_client: AsyncClient):
        """Test that invalid status is rejected."""
        response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Test", "status": "invalid"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rating_validation(self, authenticated_client: AsyncClient):
        """Test rating must be between 0 and 5."""
        # Create media first
        create_response = await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Test", "status": "finished"},
        )
        media_id = create_response.json()["id"]

        # Try invalid rating
        response = await authenticated_client.patch(
            f"/api/media/{media_id}",
            json={"rating": 10},
        )
        assert response.status_code == 422


class TestTagsEndpoints:
    """Tests for tags endpoints."""

    @pytest.mark.asyncio
    async def test_list_tags_empty(self, authenticated_client: AsyncClient):
        """Test listing tags when none exist."""
        response = await authenticated_client.get("/api/media/tags/list")
        assert response.status_code == 200
        data = response.json()
        assert data == []

    @pytest.mark.asyncio
    async def test_create_tag(self, authenticated_client: AsyncClient):
        """Test creating a tag."""
        response = await authenticated_client.post(
            "/api/media/tags",
            json={"name": "Favorites"},
        )
        assert response.status_code in [200, 201]
        data = response.json()
        assert data["name"] == "Favorites"

    @pytest.mark.asyncio
    async def test_create_media_with_tags(self, authenticated_client: AsyncClient):
        """Test creating media with tags."""
        # Tags are passed as query params
        response = await authenticated_client.post(
            "/api/media?tags=Must%20Watch&tags=Recommended",
            json={
                "type": "film",
                "title": "Tagged Movie",
                "status": "to_consume",
            },
        )
        assert response.status_code == 201
        # Note: Tags may or may not be included in response depending on implementation


class TestIncompleteMediaEndpoint:
    """Tests for incomplete media endpoint."""

    @pytest.mark.asyncio
    async def test_list_incomplete_media(self, authenticated_client: AsyncClient):
        """Test listing media with missing fields."""
        # Create complete media
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Complete Movie",
                "year": 2024,
                "status": "finished",
                "rating": 4.0,
            },
        )

        # Create incomplete media (missing rating for finished)
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Incomplete Movie",
                "status": "finished",
            },
        )

        response = await authenticated_client.get("/api/media/incomplete")
        assert response.status_code == 200
        data = response.json()
        # Should find the incomplete one
        assert len(data["items"]) >= 1
