"""Template context helpers."""

from functools import partial
from typing import Any

from fastapi import Request

from src.i18n import t
from src.models.user import User


def get_base_context(request: Request, user: User | None = None) -> dict[str, Any]:
    """Get base context for all templates."""
    locale = user.locale if user else request.session.get("locale", "en")

    # Determine current page from URL path for navbar highlighting
    path = request.url.path
    if path == "/":
        current_page = "dashboard"
    elif path.startswith("/catalogue"):
        current_page = "catalogue"
    elif path.startswith("/add"):
        current_page = "add"
    elif path.startswith("/stats"):
        current_page = "stats"
    elif path.startswith("/settings"):
        current_page = "settings"
    else:
        current_page = None

    return {
        "request": request,
        "user": user,
        "locale": locale,
        "t": partial(t, locale=locale),  # Translation function bound to current locale
        "current_page": current_page,
    }
