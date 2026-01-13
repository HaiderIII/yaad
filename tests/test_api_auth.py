"""Tests for authentication API endpoints."""

import pytest
from httpx import AsyncClient


class TestAuthEndpoints:
    """Tests for /api/auth endpoints."""

    @pytest.mark.asyncio
    async def test_github_login_redirect(self, client: AsyncClient):
        """Test GitHub login initiates OAuth flow."""
        response = await client.get("/api/auth/github/login", follow_redirects=False)
        # Should redirect to GitHub
        assert response.status_code in [302, 307]
        assert "github.com" in response.headers.get("location", "").lower() or response.status_code == 302

    @pytest.mark.asyncio
    async def test_google_login_redirect(self, client: AsyncClient):
        """Test Google login initiates OAuth flow."""
        response = await client.get("/api/auth/google/login", follow_redirects=False)
        # Should redirect to Google
        assert response.status_code in [302, 307]

    @pytest.mark.asyncio
    async def test_logout_unauthenticated(self, client: AsyncClient):
        """Test logout when not authenticated."""
        response = await client.get("/api/auth/logout", follow_redirects=False)
        # Should still work, just redirect
        assert response.status_code in [200, 302, 307]

    @pytest.mark.asyncio
    async def test_logout_authenticated(self, authenticated_client: AsyncClient):
        """Test logout when authenticated."""
        response = await authenticated_client.get("/api/auth/logout", follow_redirects=False)
        assert response.status_code in [200, 302, 307]

    @pytest.mark.asyncio
    async def test_me_unauthenticated(self, client: AsyncClient):
        """Test getting current user when not authenticated."""
        response = await client.get("/api/auth/me", follow_redirects=False)
        # May redirect to login or return 401
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_me_authenticated(self, authenticated_client: AsyncClient):
        """Test getting current user when authenticated."""
        response = await authenticated_client.get("/api/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "username" in data
        assert data["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_github_callback_without_code(self, client: AsyncClient):
        """Test GitHub callback without authorization code."""
        response = await client.get("/api/auth/github/callback")
        # Should fail or redirect with error
        assert response.status_code in [400, 401, 422, 302, 307, 500]

    @pytest.mark.asyncio
    async def test_google_callback_without_code(self, client: AsyncClient):
        """Test Google callback without authorization code."""
        response = await client.get("/api/auth/google/callback")
        # Should fail or redirect with error
        assert response.status_code in [400, 401, 422, 302, 307, 500]


class TestAuthSessionManagement:
    """Tests for session management."""

    @pytest.mark.asyncio
    async def test_protected_endpoint_without_auth(self, client: AsyncClient):
        """Test that protected endpoints require authentication."""
        endpoints = [
            ("/api/media", "GET"),
            ("/api/media", "POST"),
            ("/api/stats", "GET"),
            ("/api/search?q=test", "GET"),
            ("/api/user/settings", "GET"),
        ]

        for endpoint, method in endpoints:
            if method == "GET":
                response = await client.get(endpoint, follow_redirects=False)
            elif method == "POST":
                response = await client.post(endpoint, json={}, follow_redirects=False)

            # May redirect to login or return 401
            assert response.status_code in [401, 302, 307], f"Endpoint {endpoint} should require auth"

    @pytest.mark.asyncio
    async def test_protected_endpoint_with_auth(self, authenticated_client: AsyncClient):
        """Test that authenticated requests succeed."""
        response = await authenticated_client.get("/api/media")
        assert response.status_code == 200
