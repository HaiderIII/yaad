"""Template context helpers."""

from typing import Any

from fastapi import Request

from src.models.user import User


def get_base_context(request: Request, user: User | None = None) -> dict[str, Any]:
    """Get base context for all templates."""
    return {
        "request": request,
        "user": user,
        "locale": user.locale if user else request.session.get("locale", "en"),
    }
