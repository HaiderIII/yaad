"""Rate limiter for external API calls."""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests_per_second: float = 2.0  # Max requests per second
    burst_size: int = 5  # Allow short bursts
    min_interval: float = 0.1  # Minimum time between requests (seconds)


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""

    capacity: int
    refill_rate: float  # tokens per second
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.tokens = float(self.capacity)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def acquire(self, tokens: int = 1) -> float:
        """Try to acquire tokens.

        Returns:
            Wait time in seconds (0 if tokens acquired immediately)
        """
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return 0.0

        # Calculate wait time
        tokens_needed = tokens - self.tokens
        wait_time = tokens_needed / self.refill_rate
        return wait_time

    async def acquire_async(self, tokens: int = 1) -> None:
        """Acquire tokens, waiting if necessary."""
        wait_time = self.acquire(tokens)
        if wait_time > 0:
            logger.debug(f"Rate limited, waiting {wait_time:.2f}s")
            await asyncio.sleep(wait_time)
            self._refill()
            self.tokens -= tokens


class RateLimiter:
    """Global rate limiter for external API calls.

    Uses token bucket algorithm with per-service limits.
    Thread-safe for async usage.
    """

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._configs: dict[str, RateLimitConfig] = {}
        self._lock = asyncio.Lock()
        self._last_request: dict[str, float] = defaultdict(float)

        # Default configurations for known services
        self._default_configs = {
            "tmdb": RateLimitConfig(requests_per_second=4.0, burst_size=10),
            "justwatch": RateLimitConfig(requests_per_second=1.0, burst_size=3),
            "openlibrary": RateLimitConfig(requests_per_second=2.0, burst_size=5),
            "letterboxd": RateLimitConfig(requests_per_second=1.0, burst_size=2),
            "youtube": RateLimitConfig(requests_per_second=2.0, burst_size=5),
            "default": RateLimitConfig(requests_per_second=2.0, burst_size=5),
        }

    def configure(self, service: str, config: RateLimitConfig) -> None:
        """Configure rate limits for a specific service."""
        self._configs[service] = config
        # Reset bucket with new config
        if service in self._buckets:
            del self._buckets[service]

    def _get_bucket(self, service: str) -> TokenBucket:
        """Get or create a token bucket for a service."""
        if service not in self._buckets:
            config = self._configs.get(
                service, self._default_configs.get(service, self._default_configs["default"])
            )
            self._buckets[service] = TokenBucket(
                capacity=config.burst_size,
                refill_rate=config.requests_per_second,
            )
        return self._buckets[service]

    def _get_min_interval(self, service: str) -> float:
        """Get minimum interval for a service."""
        config = self._configs.get(
            service, self._default_configs.get(service, self._default_configs["default"])
        )
        return config.min_interval

    async def acquire(self, service: str = "default", tokens: int = 1) -> None:
        """Acquire rate limit tokens for a service.

        This method will block if rate limit is exceeded.

        Args:
            service: Name of the service (tmdb, justwatch, etc.)
            tokens: Number of tokens to acquire (default 1)
        """
        async with self._lock:
            bucket = self._get_bucket(service)
            min_interval = self._get_min_interval(service)

            # Ensure minimum interval between requests
            now = time.monotonic()
            elapsed = now - self._last_request[service]
            if elapsed < min_interval:
                wait = min_interval - elapsed
                logger.debug(f"Rate limit [{service}]: waiting {wait:.3f}s (min interval)")
                await asyncio.sleep(wait)

            # Acquire from token bucket
            await bucket.acquire_async(tokens)
            self._last_request[service] = time.monotonic()

    async def __aenter__(self) -> "RateLimiter":
        """Context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Context manager exit."""
        pass

    def get_stats(self) -> dict[str, dict[str, float]]:
        """Get current rate limiter statistics."""
        stats = {}
        for service, bucket in self._buckets.items():
            bucket._refill()
            stats[service] = {
                "available_tokens": bucket.tokens,
                "capacity": bucket.capacity,
                "refill_rate": bucket.refill_rate,
            }
        return stats


# Global rate limiter instance
rate_limiter = RateLimiter()


async def rate_limited(service: str = "default"):
    """Decorator/context manager for rate-limited API calls.

    Usage as context manager:
        async with rate_limited("tmdb"):
            response = await client.get(url)

    Usage as decorator:
        @rate_limited("tmdb")
        async def fetch_movie(movie_id: int):
            ...
    """
    await rate_limiter.acquire(service)


class RateLimitedClient:
    """Wrapper for httpx client with automatic rate limiting."""

    def __init__(self, client: Any, service: str = "default"):
        self._client = client
        self._service = service

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Rate-limited GET request."""
        await rate_limiter.acquire(self._service)
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        """Rate-limited POST request."""
        await rate_limiter.acquire(self._service)
        return await self._client.post(url, **kwargs)

    async def __aenter__(self) -> "RateLimitedClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)
