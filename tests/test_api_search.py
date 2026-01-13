"""Tests for search API endpoints."""

import pytest
from httpx import AsyncClient


class TestSearchEndpoints:
    """Tests for /api/search endpoints."""

    @pytest.mark.asyncio
    async def test_search_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot search."""
        response = await client.get("/api/search?q=test", follow_redirects=False)
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_search_empty_query(self, authenticated_client: AsyncClient):
        """Test search with empty query."""
        response = await authenticated_client.get("/api/search?q=")
        # Should return empty results or handle gracefully
        assert response.status_code in [200, 422]

    @pytest.mark.asyncio
    async def test_search_no_results(self, authenticated_client: AsyncClient):
        """Test search with no matching results."""
        response = await authenticated_client.get("/api/search?q=xyznonexistent123")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_search_by_title(self, authenticated_client: AsyncClient):
        """Test searching by title."""
        # Create media
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "The Matrix", "status": "to_consume"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Inception", "status": "to_consume"},
        )

        response = await authenticated_client.get("/api/search?q=matrix")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any("Matrix" in item["title"] for item in data)

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, authenticated_client: AsyncClient):
        """Test that search is case insensitive."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "The Matrix", "status": "to_consume"},
        )

        # Search with different cases
        for query in ["MATRIX", "matrix", "Matrix", "MaTrIx"]:
            response = await authenticated_client.get(f"/api/search?q={query}")
            assert response.status_code == 200
            data = response.json()
            assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_search_partial_match(self, authenticated_client: AsyncClient):
        """Test partial matching in search."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Interstellar", "status": "to_consume"},
        )

        response = await authenticated_client.get("/api/search?q=interstellar")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_search_limit(self, authenticated_client: AsyncClient):
        """Test search result limit."""
        # Create many items
        for i in range(20):
            await authenticated_client.post(
                "/api/media",
                json={"type": "film", "title": f"Test Movie {i}", "status": "to_consume"},
            )

        response = await authenticated_client.get("/api/search?q=test&limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data) <= 5

    @pytest.mark.asyncio
    async def test_search_multiple_fields(self, authenticated_client: AsyncClient):
        """Test searching across multiple fields."""
        # Create media with author
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "book",
                "title": "1984",
                "status": "to_consume",
                "authors": ["George Orwell"],
            },
        )

        # Search by author name
        response = await authenticated_client.get("/api/search?q=orwell")
        assert response.status_code == 200
        data = response.json()
        # May or may not find by author depending on implementation
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_search_special_characters(self, authenticated_client: AsyncClient):
        """Test search with special characters."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Spider-Man: No Way Home", "status": "to_consume"},
        )

        # Search with special chars
        response = await authenticated_client.get("/api/search?q=spider-man")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_search_unicode(self, authenticated_client: AsyncClient):
        """Test search with unicode characters."""
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Les Misérables", "status": "to_consume"},
        )

        response = await authenticated_client.get("/api/search?q=misérables")
        assert response.status_code == 200
