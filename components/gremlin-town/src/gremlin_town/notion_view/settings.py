"""Configuration for `[notion_view]` (docs/product/steps/h-notion-view.md §5): off unless enabled.

Mirrors `review/settings.py`'s TOML-table style: unknown keys are rejected and every default is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_KEYS = frozenset({
    "enabled", "token_file", "database_id", "state_file", "check_seconds", "repair_hours", "done_days",
})
DEFAULT_CHECK_SECONDS = 60
DEFAULT_REPAIR_HOURS = 24
DEFAULT_DONE_DAYS = 14


class NotionViewSettingsError(ValueError):
    """The `[notion_view]` table is malformed."""


def _positive(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise NotionViewSettingsError(f"[notion_view] {key} must be a positive integer")
    return value


def _path(table: dict[str, Any], key: str, base: Path) -> Path | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise NotionViewSettingsError(f"[notion_view] {key} must be a non-empty string")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


@dataclass(frozen=True, slots=True)
class NotionViewSettings:
    enabled: bool
    token_file: Path | None = None
    database_id: str | None = None
    state_file: Path | None = None
    check_seconds: int = DEFAULT_CHECK_SECONDS
    repair_hours: int = DEFAULT_REPAIR_HOURS
    done_days: int = DEFAULT_DONE_DAYS

    @classmethod
    def from_table(cls, table: dict[str, Any] | None, *, base: Path) -> "NotionViewSettings":
        """Build settings from a parsed `[notion_view]` table, or its absence; relative paths resolve from ``base``."""

        if table is None:
            return cls(enabled=False)
        if not isinstance(table, dict):
            raise NotionViewSettingsError("[notion_view] must be a table")
        unknown = set(table) - ALLOWED_KEYS
        if unknown:
            raise NotionViewSettingsError(f"[notion_view] has unknown keys: {sorted(unknown)}")
        enabled = table.get("enabled", False)
        if not isinstance(enabled, bool):
            raise NotionViewSettingsError("[notion_view] enabled must be a boolean")
        database_id = table.get("database_id")
        if database_id is not None and (not isinstance(database_id, str) or not database_id.strip()):
            raise NotionViewSettingsError("[notion_view] database_id must be a non-empty string")
        settings = cls(
            enabled=enabled,
            token_file=_path(table, "token_file", base),
            database_id=database_id.strip() if database_id else None,
            state_file=_path(table, "state_file", base),
            check_seconds=_positive(table, "check_seconds", DEFAULT_CHECK_SECONDS),
            repair_hours=_positive(table, "repair_hours", DEFAULT_REPAIR_HOURS),
            done_days=_positive(table, "done_days", DEFAULT_DONE_DAYS),
        )
        if enabled and settings.state_file is None:
            raise NotionViewSettingsError("[notion_view] enabled requires state_file")
        return settings
