"""Authentication module."""

from src.auth.dependencies import get_current_user, get_optional_user
from src.auth.oauth import oauth

__all__ = [
    "get_current_user",
    "get_optional_user",
    "oauth",
]
