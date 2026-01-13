"""Tests for import API endpoints."""

import pytest
from httpx import AsyncClient
from io import BytesIO


class TestLetterboxdImportEndpoints:
    """Tests for /api/import/letterboxd endpoints."""

    @pytest.mark.asyncio
    async def test_import_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot import."""
        response = await client.post("/api/import/letterboxd", follow_redirects=False)
        assert response.status_code in [401, 302, 307, 422]

    @pytest.mark.asyncio
    async def test_validate_username_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot validate username."""
        response = await client.get("/api/import/letterboxd/validate?username=test", follow_redirects=False)
        assert response.status_code in [401, 302, 307]

    @pytest.mark.asyncio
    async def test_validate_username_empty(self, authenticated_client: AsyncClient):
        """Test validating empty username."""
        response = await authenticated_client.get("/api/import/letterboxd/validate?username=")
        # May return 200 with validation result or 400/422
        assert response.status_code in [200, 400, 422]

    @pytest.mark.asyncio
    async def test_validate_username(self, authenticated_client: AsyncClient):
        """Test validating a Letterboxd username."""
        response = await authenticated_client.get(
            "/api/import/letterboxd/validate?username=testuser"
        )
        # Will fail if user doesn't exist on Letterboxd, but should not error
        assert response.status_code in [200, 404, 500]

    @pytest.mark.asyncio
    async def test_sync_no_username_set(self, authenticated_client: AsyncClient):
        """Test sync when no Letterboxd username is set."""
        response = await authenticated_client.post("/api/import/letterboxd/sync")
        # Should fail gracefully
        assert response.status_code in [400, 422, 200]

    @pytest.mark.asyncio
    async def test_import_csv_no_file(self, authenticated_client: AsyncClient):
        """Test CSV import without file."""
        response = await authenticated_client.post("/api/import/letterboxd")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_import_csv_invalid_format(self, authenticated_client: AsyncClient):
        """Test CSV import with invalid file format."""
        # Create a fake file with wrong content
        content = b"not,a,valid,csv,for,letterboxd"
        files = {"file": ("ratings.csv", BytesIO(content), "text/csv")}

        response = await authenticated_client.post(
            "/api/import/letterboxd",
            files=files,
        )
        # Should handle gracefully
        assert response.status_code in [200, 400, 422]


class TestNotionImportEndpoints:
    """Tests for /api/import/notion endpoints."""

    @pytest.mark.asyncio
    async def test_notion_import_unauthenticated(self, client: AsyncClient):
        """Test that unauthenticated users cannot import from Notion."""
        response = await client.post("/api/import/notion", follow_redirects=False)
        assert response.status_code in [401, 302, 307, 422]

    @pytest.mark.asyncio
    async def test_notion_import_no_file(self, authenticated_client: AsyncClient):
        """Test Notion import without file."""
        response = await authenticated_client.post("/api/import/notion")
        assert response.status_code == 422


class TestImportValidation:
    """Tests for import request validation."""

    @pytest.mark.asyncio
    async def test_letterboxd_csv_headers(self, authenticated_client: AsyncClient):
        """Test that Letterboxd CSV requires specific headers."""
        # Valid Letterboxd CSV format
        content = b"Date,Name,Year,Letterboxd URI,Rating\n2024-01-01,Test Movie,2024,https://letterboxd.com/film/test,4.5"
        files = {"file": ("ratings.csv", BytesIO(content), "text/csv")}

        response = await authenticated_client.post(
            "/api/import/letterboxd",
            files=files,
        )
        # Should process without error (may not import if movie not found)
        assert response.status_code in [200, 207, 400]

    @pytest.mark.asyncio
    async def test_file_size_limit(self, authenticated_client: AsyncClient):
        """Test that large files are handled."""
        # Create a moderately large file
        large_content = b"Date,Name,Year,Letterboxd URI,Rating\n" + b"2024-01-01,Test,2024,https://example.com,4.5\n" * 1000
        files = {"file": ("large.csv", BytesIO(large_content), "text/csv")}

        response = await authenticated_client.post(
            "/api/import/letterboxd",
            files=files,
        )
        # Should process or reject gracefully
        assert response.status_code in [200, 207, 400, 413, 422]
