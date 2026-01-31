"""Shared persistent httpx clients for external API calls.

Using persistent clients avoids creating a new TCP connection + TLS handshake
for every API call, improving performance through connection reuse and pooling.
"""

import httpx

from src.constants import API_TIMEOUT_EXTERNAL, HTTPX_TIMEOUT

# Connection pool limits
_POOL_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=30,
)

# Shared clients for different service groups
_tmdb_client: httpx.AsyncClient | None = None
_general_client: httpx.AsyncClient | None = None


def get_tmdb_client() -> httpx.AsyncClient:
    """Get persistent httpx client for TMDB API calls."""
    global _tmdb_client
    if _tmdb_client is None:
        _tmdb_client = httpx.AsyncClient(
            timeout=HTTPX_TIMEOUT,
            limits=_POOL_LIMITS,
            http2=False,
        )
    return _tmdb_client


def get_general_client() -> httpx.AsyncClient:
    """Get persistent httpx client for general API calls (JustWatch, Books, YouTube, Kobo, etc.)."""
    global _general_client
    if _general_client is None:
        _general_client = httpx.AsyncClient(
            timeout=API_TIMEOUT_EXTERNAL,
            limits=_POOL_LIMITS,
            http2=False,
        )
    return _general_client


async def close_all_clients() -> None:
    """Close all persistent httpx clients. Call during app shutdown."""
    global _tmdb_client, _general_client
    if _tmdb_client is not None:
        await _tmdb_client.aclose()
        _tmdb_client = None
    if _general_client is not None:
        await _general_client.aclose()
        _general_client = None
