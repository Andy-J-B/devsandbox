"""Small JSON-normalization helper for audit event payloads."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def jsonable(value: Any) -> Any:
    """Return a JSON-column-safe copy of ``value``.

    Transition fields can contain datetimes, enums, and Pydantic models; the
    audit event payload must remain serializable even though the same fields
    are written to typed ORM columns.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return value
