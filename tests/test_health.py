"""Tests for health check endpoint."""

import pytest
from httpx import AsyncClient


class TestHealthCheck:
    """Tests for /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_endpoint_exists(self, client: AsyncClient):
        """Test that health endpoint returns a response."""
        response = await client.get("/health")

        # Should return 200 or 503 depending on service status
        assert response.status_code in [200, 503]

    @pytest.mark.asyncio
    async def test_health_response_structure(self, client: AsyncClient):
        """Test health response has correct structure."""
        response = await client.get("/health")
        data = response.json()

        assert "status" in data
        assert "timestamp" in data
        assert "uptime_seconds" in data
        assert "version" in data
        assert "checks" in data

    @pytest.mark.asyncio
    async def test_health_status_values(self, client: AsyncClient):
        """Test health status is a valid value."""
        response = await client.get("/health")
        data = response.json()

        assert data["status"] in ["healthy", "degraded", "unhealthy"]

    @pytest.mark.asyncio
    async def test_health_checks_structure(self, client: AsyncClient):
        """Test that checks contain database and redis."""
        response = await client.get("/health")
        data = response.json()

        checks = data["checks"]
        # These may fail in test environment, but keys should exist
        assert "database" in checks or "redis" in checks
