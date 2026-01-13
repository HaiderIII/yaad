"""Secure secrets management with rotation support.

This module provides utilities for:
- Secure secret validation
- API key rotation without downtime
- Secret health monitoring
"""

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SecretKey:
    """Represents a secret key with metadata."""

    value: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    is_primary: bool = True

    @property
    def is_expired(self) -> bool:
        """Check if the key has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at

    @property
    def age_days(self) -> int:
        """Get the age of the key in days."""
        return (datetime.now(UTC) - self.created_at).days

    def should_rotate(self, max_age_days: int = 90) -> bool:
        """Check if the key should be rotated based on age."""
        return self.age_days >= max_age_days


class SecretManager:
    """Manages secrets with support for rotation.

    Supports multiple active keys for zero-downtime rotation:
    - Primary key: Used for new operations
    - Secondary keys: Accepted for validation during transition

    Usage:
        manager = SecretManager()
        manager.add_key("primary_secret", is_primary=True)
        manager.add_key("old_secret", is_primary=False)  # Still valid during rotation

        # Validate against any active key
        if manager.validate_signature(data, signature):
            ...

        # Create new signature with primary key
        signature = manager.sign(data)
    """

    def __init__(self, max_keys: int = 3):
        self._keys: list[SecretKey] = []
        self._max_keys = max_keys

    def add_key(
        self,
        value: str,
        is_primary: bool = False,
        expires_in_days: int | None = None,
    ) -> None:
        """Add a new secret key.

        Args:
            value: The secret key value
            is_primary: Whether this is the primary key for new operations
            expires_in_days: Optional expiration in days
        """
        if not value or len(value) < 32:
            raise ValueError("Secret key must be at least 32 characters")

        expires_at = None
        if expires_in_days:
            expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

        # If setting as primary, demote existing primary
        if is_primary:
            for key in self._keys:
                key.is_primary = False

        key = SecretKey(
            value=value,
            expires_at=expires_at,
            is_primary=is_primary,
        )
        self._keys.append(key)

        # Maintain max keys limit (remove oldest non-primary)
        while len(self._keys) > self._max_keys:
            for i, k in enumerate(self._keys):
                if not k.is_primary:
                    self._keys.pop(i)
                    break

        logger.info(f"Added {'primary' if is_primary else 'secondary'} secret key")

    def get_primary_key(self) -> str | None:
        """Get the primary secret key value."""
        for key in self._keys:
            if key.is_primary and not key.is_expired:
                return key.value
        return None

    def get_active_keys(self) -> list[str]:
        """Get all non-expired key values."""
        return [k.value for k in self._keys if not k.is_expired]

    def sign(self, data: str | bytes) -> str:
        """Create HMAC signature using primary key.

        Args:
            data: Data to sign

        Returns:
            Hex-encoded HMAC-SHA256 signature

        Raises:
            ValueError: If no primary key is available
        """
        primary = self.get_primary_key()
        if not primary:
            raise ValueError("No primary key available for signing")

        if isinstance(data, str):
            data = data.encode("utf-8")

        return hmac.new(
            primary.encode("utf-8"),
            data,
            hashlib.sha256,
        ).hexdigest()

    def validate_signature(self, data: str | bytes, signature: str) -> bool:
        """Validate signature against any active key.

        This allows signature validation during key rotation,
        accepting signatures made with any non-expired key.

        Args:
            data: Original data
            signature: Hex-encoded signature to validate

        Returns:
            True if signature is valid with any active key
        """
        if isinstance(data, str):
            data = data.encode("utf-8")

        for key_value in self.get_active_keys():
            expected = hmac.new(
                key_value.encode("utf-8"),
                data,
                hashlib.sha256,
            ).hexdigest()

            if hmac.compare_digest(expected, signature):
                return True

        return False

    def cleanup_expired(self) -> int:
        """Remove expired keys.

        Returns:
            Number of keys removed
        """
        before = len(self._keys)
        self._keys = [k for k in self._keys if not k.is_expired]
        removed = before - len(self._keys)
        if removed:
            logger.info(f"Cleaned up {removed} expired secret keys")
        return removed

    def get_rotation_status(self) -> dict[str, Any]:
        """Get status of all keys for monitoring.

        Returns:
            Dict with key metadata (without exposing values)
        """
        return {
            "total_keys": len(self._keys),
            "active_keys": len(self.get_active_keys()),
            "keys": [
                {
                    "is_primary": k.is_primary,
                    "age_days": k.age_days,
                    "is_expired": k.is_expired,
                    "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                    "needs_rotation": k.should_rotate(),
                }
                for k in self._keys
            ],
        }


def generate_secure_key(length: int = 64) -> str:
    """Generate a cryptographically secure random key.

    Args:
        length: Length of the key in characters

    Returns:
        URL-safe base64-encoded random string
    """
    return secrets.token_urlsafe(length)


def validate_secret_strength(secret: str, min_length: int = 32) -> tuple[bool, list[str]]:
    """Validate the strength of a secret.

    Args:
        secret: Secret to validate
        min_length: Minimum required length

    Returns:
        Tuple of (is_valid, list of issues)
    """
    issues = []

    if len(secret) < min_length:
        issues.append(f"Secret must be at least {min_length} characters")

    # Check for common weak patterns
    weak_patterns = [
        "password",
        "secret",
        "12345",
        "qwerty",
        "admin",
        "letmein",
    ]
    secret_lower = secret.lower()
    for pattern in weak_patterns:
        if pattern in secret_lower:
            issues.append(f"Secret contains weak pattern: {pattern}")

    # Check entropy (simple check: should have mixed characters)
    has_upper = any(c.isupper() for c in secret)
    has_lower = any(c.islower() for c in secret)
    has_digit = any(c.isdigit() for c in secret)

    if not (has_upper and has_lower and has_digit):
        issues.append("Secret should contain uppercase, lowercase, and digits")

    return len(issues) == 0, issues


def mask_secret(secret: str, visible_chars: int = 4) -> str:
    """Mask a secret for logging purposes.

    Args:
        secret: Secret to mask
        visible_chars: Number of characters to show at start and end

    Returns:
        Masked secret like "abc...xyz"
    """
    if len(secret) <= visible_chars * 2:
        return "*" * len(secret)
    return f"{secret[:visible_chars]}...{secret[-visible_chars:]}"


class APIKeyRotator:
    """Helper for rotating external API keys.

    Tracks API keys and their usage to facilitate rotation.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._current_key: str | None = None
        self._previous_key: str | None = None
        self._rotated_at: datetime | None = None

    def set_key(self, key: str) -> None:
        """Set the current API key."""
        if self._current_key and self._current_key != key:
            self._previous_key = self._current_key
            self._rotated_at = datetime.now(UTC)
            logger.info(f"Rotated API key for {self.service_name}")
        self._current_key = key

    def get_key(self) -> str | None:
        """Get the current API key."""
        return self._current_key

    def get_fallback_key(self) -> str | None:
        """Get the previous key as fallback during rotation."""
        # Only return previous key if rotation was recent (within 1 hour)
        if self._previous_key and self._rotated_at:
            if datetime.now(UTC) - self._rotated_at < timedelta(hours=1):
                return self._previous_key
        return None

    def get_status(self) -> dict[str, Any]:
        """Get rotation status."""
        return {
            "service": self.service_name,
            "has_current_key": self._current_key is not None,
            "has_fallback": self.get_fallback_key() is not None,
            "last_rotation": self._rotated_at.isoformat() if self._rotated_at else None,
        }
