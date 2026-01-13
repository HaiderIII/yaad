"""Cursor-based pagination utilities.

Provides efficient cursor-based pagination for large datasets,
avoiding the performance issues of offset-based pagination.
"""

import base64
import json
from datetime import datetime
from typing import Any


def encode_cursor(values: dict[str, Any]) -> str:
    """Encode cursor values to a base64 string.

    Args:
        values: Dict of column name -> value pairs for cursor position

    Returns:
        Base64-encoded cursor string
    """
    # Convert datetime to ISO format for JSON serialization
    serializable = {}
    for key, value in values.items():
        if isinstance(value, datetime):
            serializable[key] = {"_type": "datetime", "value": value.isoformat()}
        else:
            serializable[key] = value

    json_str = json.dumps(serializable, sort_keys=True)
    return base64.urlsafe_b64encode(json_str.encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any] | None:
    """Decode a cursor string back to values.

    Args:
        cursor: Base64-encoded cursor string

    Returns:
        Dict of column name -> value pairs, or None if invalid
    """
    try:
        json_str = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(json_str)

        # Convert datetime strings back to datetime objects
        result = {}
        for key, value in data.items():
            if isinstance(value, dict) and value.get("_type") == "datetime":
                result[key] = datetime.fromisoformat(value["value"])
            else:
                result[key] = value

        return result
    except (ValueError, json.JSONDecodeError, KeyError):
        return None


def create_cursor_from_item(item: Any, sort_by: str) -> str:
    """Create a cursor from a database item.

    Args:
        item: Database model instance
        sort_by: The field name used for sorting

    Returns:
        Encoded cursor string
    """
    sort_value = getattr(item, sort_by, None)
    item_id = getattr(item, "id", None)

    return encode_cursor({
        "sort_value": sort_value,
        "id": item_id,
        "sort_by": sort_by,
    })
