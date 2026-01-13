"""Centralized logging configuration for the Yaad application."""

import logging
import sys
from typing import Literal

from src.config import get_settings


def setup_logging(
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
) -> None:
    """Configure logging for the application.

    Args:
        level: Override log level (default: INFO for production, DEBUG for development)
    """
    settings = get_settings()

    if level is None:
        level = "INFO" if settings.is_production else "DEBUG"

    # Root logger configuration
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name.

    This is a thin wrapper around logging.getLogger that ensures
    consistent naming and configuration.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


class LogContext:
    """Context manager for adding structured context to log messages."""

    def __init__(self, logger: logging.Logger, **context: str) -> None:
        """Initialize the log context.

        Args:
            logger: The logger to use
            **context: Key-value pairs to include in log messages
        """
        self.logger = logger
        self.context = context
        self.prefix = " ".join(f"[{k}={v}]" for k, v in context.items())

    def debug(self, msg: str, *args, **kwargs) -> None:
        """Log a debug message with context."""
        self.logger.debug(f"{self.prefix} {msg}", *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        """Log an info message with context."""
        self.logger.info(f"{self.prefix} {msg}", *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        """Log a warning message with context."""
        self.logger.warning(f"{self.prefix} {msg}", *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        """Log an error message with context."""
        self.logger.error(f"{self.prefix} {msg}", *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        """Log a critical message with context."""
        self.logger.critical(f"{self.prefix} {msg}", *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        """Log an exception with context."""
        self.logger.exception(f"{self.prefix} {msg}", *args, **kwargs)
