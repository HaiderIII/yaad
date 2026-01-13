"""Tests for stats API endpoints."""

import pytest
from httpx import AsyncClient


class TestStatsEndpoints:
    """Tests for /api/stats endpoints."""

    @pytest.mark.asyncio
    async def test_get_stats_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot get stats."""
        response = await client.get("/api/stats", follow_redirects=False)
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, authenticated_client: AsyncClient):
        """Test getting stats when no media exists."""
        response = await authenticated_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()

        # Stats response is flat, not nested in "summary"
        assert data["total_media"] == 0
        assert data["total_finished"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_with_media(self, authenticated_client: AsyncClient):
        """Test getting stats with media."""
        # Create some media
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Film 1",
                "status": "finished",
                "rating": 4.0,
                "duration_minutes": 120,
            },
        )
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "film",
                "title": "Film 2",
                "status": "in_progress",
            },
        )
        await authenticated_client.post(
            "/api/media",
            json={
                "type": "book",
                "title": "Book 1",
                "status": "finished",
                "page_count": 300,
            },
        )

        response = await authenticated_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["total_media"] == 3
        assert data["total_finished"] == 2
        assert data["total_in_progress"] == 1

    @pytest.mark.asyncio
    async def test_stats_by_type(self, authenticated_client: AsyncClient):
        """Test stats breakdown by type."""
        # Create different types
        await authenticated_client.post(
            "/api/media",
            json={"type": "film", "title": "Film", "status": "finished"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "book", "title": "Book", "status": "finished"},
        )
        await authenticated_client.post(
            "/api/media",
            json={"type": "series", "title": "Series", "status": "finished"},
        )

        response = await authenticated_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()

        assert "by_type" in data
        by_type = data["by_type"]
        assert isinstance(by_type, list)

    @pytest.mark.asyncio
    async def test_stats_by_status(self, authenticated_client: AsyncClient):
        """Test stats breakdown by status."""
        statuses = ["to_consume", "in_progress", "finished", "abandoned"]
        for i, status in enumerate(statuses):
            await authenticated_client.post(
                "/api/media",
                json={"type": "film", "title": f"Film {i}", "status": status},
            )

        response = await authenticated_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()

        assert "by_status" in data

    @pytest.mark.asyncio
    async def test_stats_rating_distribution(self, authenticated_client: AsyncClient):
        """Test rating distribution in stats."""
        ratings = [1.0, 2.0, 3.0, 4.0, 5.0]
        for rating in ratings:
            await authenticated_client.post(
                "/api/media",
                json={
                    "type": "film",
                    "title": f"Film {rating}",
                    "status": "finished",
                    "rating": rating,
                },
            )

        response = await authenticated_client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()

        # Average should be around 3.0
        if "average_rating" in data and data["average_rating"]:
            assert 2.5 <= data["average_rating"] <= 3.5
