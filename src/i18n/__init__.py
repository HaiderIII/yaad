"""Internationalization module."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

I18N_DIR = Path(__file__).parent


@lru_cache
def load_translations(locale: str) -> dict[str, Any]:
    """Load translations for a given locale."""
    file_path = I18N_DIR / f"{locale}.json"
    if not file_path.exists():
        file_path = I18N_DIR / "en.json"

    with open(file_path) as f:
        return json.load(f)


def get_translation(key: str, locale: str = "en") -> str:
    """Get a translation for a key."""
    translations = load_translations(locale)
    keys = key.split(".")
    value = translations

    for k in keys:
        if isinstance(value, dict):
            value = value.get(k, key)
        else:
            return key

    return str(value) if not isinstance(value, dict) else key


def t(key: str, locale: str = "en", **kwargs: Any) -> str:
    """Shorthand for get_translation with formatting support."""
    translation = get_translation(key, locale)
    if kwargs:
        try:
            return translation.format(**kwargs)
        except KeyError:
            return translation
    return translation
