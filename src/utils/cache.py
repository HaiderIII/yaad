"""Redis caching utilities for metadata and API responses.

Provides async Redis caching with automatic serialization/deserialization,
TTL management, and cache key namespacing.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any, TypeVar

import redis.asyncio as redis

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# Type for decorated functions
F = TypeVar("F", bound=Callable[..., Any])

# Cache TTL defaults
CACHE_TTL_SHORT = timedelta(minutes=15)  # Search results
CACHE_TTL_MEDIUM = timedelta(hours=6)    # Metadata details
CACHE_TTL_LONG = timedelta(hours=24)     # Streaming links, providers list


class RedisCache:
    """Async Redis cache client with JSON serialization."""

    def __init__(self) -> None:
        self._client: redis.Redis | None = None
        self._connected = False

    async def _get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = redis.from_url(
                str(settings.redis_url),
                encoding="utf-8",
                decode_responses=True,
            )
        return self._client

    async def connect(self) -> bool:
        """Test Redis connection."""
        try:
            client = await self._get_client()
            await client.ping()
            self._connected = True
            return True
        except Exception as e:
            logger.warning(f"Redis cache unavailable: {e}")
            self._connected = False
            return False

    async def ping(self) -> bool:
        """Ping Redis to check connection health."""
        client = await self._get_client()
        return await client.ping()

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None
            self._connected = False

    async def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        if not self._connected:
            return None

        try:
            client = await self._get_client()
            data = await client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.debug(f"Cache get error for {key}: {e}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: timedelta | None = None,
    ) -> bool:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache (must be JSON serializable)
            ttl: Time to live (default: 6 hours)

        Returns:
            True if successful
        """
        if not self._connected:
            return False

        try:
            client = await self._get_client()
            serialized = json.dumps(value, default=str)
            expire_seconds = int((ttl or CACHE_TTL_MEDIUM).total_seconds())
            await client.setex(key, expire_seconds, serialized)
            return True
        except Exception as e:
            logger.debug(f"Cache set error for {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if not self._connected:
            return False

        try:
            client = await self._get_client()
            await client.delete(key)
            return True
        except Exception as e:
            logger.debug(f"Cache delete error for {key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching pattern.

        Args:
            pattern: Glob pattern (e.g., "tmdb:movie:*")

        Returns:
            Number of keys deleted
        """
        if not self._connected:
            return 0

        try:
            client = await self._get_client()
            keys = []
            async for key in client.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                return await client.delete(*keys)
            return 0
        except Exception as e:
            logger.debug(f"Cache delete pattern error for {pattern}: {e}")
            return 0


# Global cache instance
cache = RedisCache()


def make_cache_key(namespace: str, *args: Any, **kwargs: Any) -> str:
    """Generate a cache key from function arguments.

    Args:
        namespace: Key prefix (e.g., "tmdb:movie")
        *args: Positional arguments to include in key
        **kwargs: Keyword arguments to include in key

    Returns:
        Cache key string
    """
    parts = [namespace]

    # Add args
    for arg in args:
        if arg is not None:
            parts.append(str(arg))

    # Add sorted kwargs
    for key, value in sorted(kwargs.items()):
        if value is not None:
            parts.append(f"{key}={value}")

    key_str = ":".join(parts)

    # Hash if too long (Redis key limit is 512MB but we want readable keys)
    if len(key_str) > 200:
        hash_suffix = hashlib.md5(key_str.encode()).hexdigest()[:12]
        key_str = f"{namespace}:{hash_suffix}"

    return key_str


def cached(
    namespace: str,
    ttl: timedelta | None = None,
    key_builder: Callable[..., str] | None = None,
) -> Callable[[F], F]:
    """Decorator to cache async function results in Redis.

    Args:
        namespace: Cache key namespace (e.g., "tmdb:movie")
        ttl: Cache TTL (default: CACHE_TTL_MEDIUM)
        key_builder: Optional custom function to build cache key

    Example:
        @cached("tmdb:movie", ttl=CACHE_TTL_LONG)
        async def get_movie_details(tmdb_id: int, language: str = "en"):
            ...
    """
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Build cache key
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                # Skip 'self' argument for methods
                cache_args = args[1:] if args and hasattr(args[0], "__class__") else args
                cache_key = make_cache_key(namespace, *cache_args, **kwargs)

            # Try to get from cache
            cached_value = await cache.get(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache HIT: {cache_key}")
                return cached_value

            # Call function
            logger.debug(f"Cache MISS: {cache_key}")
            result = await func(*args, **kwargs)

            # Cache result if not None
            if result is not None:
                await cache.set(cache_key, result, ttl or CACHE_TTL_MEDIUM)

            return result

        return wrapper  # type: ignore

    return decorator


async def invalidate_media_cache(media_type: str, external_id: str | None = None) -> None:
    """Invalidate cache for a specific media item or all items of a type.

    Args:
        media_type: "movie", "tv", "book", "youtube"
        external_id: Optional specific ID to invalidate
    """
    patterns = []

    if media_type in ("movie", "tv"):
        if external_id:
            patterns.append(f"tmdb:{media_type}:{external_id}:*")
            patterns.append(f"justwatch:{external_id}:*")
        else:
            patterns.append(f"tmdb:{media_type}:*")
            patterns.append("justwatch:*")

    elif media_type == "book":
        if external_id:
            patterns.append(f"openlibrary:{external_id}:*")
        else:
            patterns.append("openlibrary:*")

    elif media_type == "youtube":
        if external_id:
            patterns.append(f"youtube:{external_id}")
        else:
            patterns.append("youtube:*")

    for pattern in patterns:
        deleted = await cache.delete_pattern(pattern)
        if deleted:
            logger.info(f"Invalidated {deleted} cache entries for pattern {pattern}")
