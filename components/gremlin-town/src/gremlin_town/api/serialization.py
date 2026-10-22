"""Stable JSON conversion shared by API responses and the CLI."""

from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from gremlin_town.mcp.brain_runtime import MemoryListItemWithNow, MemoryRecordWithNow


def to_jsonable(value: Any) -> Any:
    """Convert contract values to JSON-safe primitives without losing labels."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "encoding": "base64",
                "value": base64.b64encode(value).decode("ascii"),
            }
    if isinstance(value, (MemoryRecordWithNow, MemoryListItemWithNow)):
        return to_jsonable(value.reader_reply_fields())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value
