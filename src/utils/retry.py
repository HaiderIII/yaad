"""Retry utilities with exponential backoff for external API calls."""

import asyncio
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 30.0  # seconds
    exponential_base: float = 2.0
    retryable_exceptions: tuple = (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.ReadError,
        ConnectionError,
        TimeoutError,
    )
    retryable_status_codes: tuple = (429, 500, 502, 503, 504)


DEFAULT_RETRY_CONFIG = RetryConfig()


async def retry_async(
    func: Callable[..., T],
    *args: Any,
    config: RetryConfig = DEFAULT_RETRY_CONFIG,
    operation_name: str = "operation",
    **kwargs: Any,
) -> T | None:
    """Execute an async function with retry logic and exponential backoff.

    Args:
        func: Async function to execute
        *args: Positional arguments for the function
        config: Retry configuration
        operation_name: Name of the operation for logging
        **kwargs: Keyword arguments for the function

    Returns:
        The result of the function, or None if all retries failed
    """
    last_exception: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            result = await func(*args, **kwargs)

            # Check for retryable HTTP status codes if result is a Response
            if isinstance(result, httpx.Response):
                if result.status_code in config.retryable_status_codes:
                    if attempt < config.max_retries:
                        delay = min(
                            config.base_delay * (config.exponential_base**attempt),
                            config.max_delay,
                        )
                        logger.warning(
                            f"{operation_name}: Got status {result.status_code}, "
                            f"retrying in {delay:.1f}s (attempt {attempt + 1}/{config.max_retries + 1})"
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(
                            f"{operation_name}: Failed after {config.max_retries + 1} attempts "
                            f"with status {result.status_code}"
                        )
                        return None

            return result

        except config.retryable_exceptions as e:
            last_exception = e
            if attempt < config.max_retries:
                delay = min(
                    config.base_delay * (config.exponential_base**attempt),
                    config.max_delay,
                )
                logger.warning(
                    f"{operation_name}: {type(e).__name__}, "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{config.max_retries + 1})"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"{operation_name}: Failed after {config.max_retries + 1} attempts: {e}"
                )

        except Exception as e:
            # Non-retryable exception
            logger.error(f"{operation_name}: Non-retryable error: {e}")
            raise

    return None


def with_retry(
    config: RetryConfig = DEFAULT_RETRY_CONFIG,
    operation_name: str | None = None,
) -> Callable:
    """Decorator to add retry logic to an async function.

    Args:
        config: Retry configuration
        operation_name: Name for logging (defaults to function name)

    Usage:
        @with_retry(config=RetryConfig(max_retries=5))
        async def fetch_data():
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T | None:
            name = operation_name or func.__name__
            return await retry_async(func, *args, config=config, operation_name=name, **kwargs)

        return wrapper

    return decorator
