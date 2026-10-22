"""Proposed operational defaults; the connector has one explicit enable gate."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_KEYS = frozenset({"enabled", "busy_calendar", "token_file", "state_file", "list_id", "admin_hours_id",
                          "deep_focus_hours_id", "errands_hours_id", "check_seconds", "max_age_seconds", "length_flow"})


@dataclass(frozen=True, slots=True)
class FlowSavvySettings:
    enabled: bool = False
    length_flow: bool = False
    busy_calendar: bool = False
    token_file: Path | None = None
    state_file: Path | None = None
    list_id: str | None = None
    admin_hours_id: str | None = None
    deep_focus_hours_id: str | None = None
    errands_hours_id: str | None = None
    check_seconds: int = 180
    max_age_seconds: int = 600

    @classmethod
    def from_table(cls, table: dict[str, Any] | None, *, base: Path) -> "FlowSavvySettings":
        if table is None:
            return cls()
        if not isinstance(table, dict) or set(table) - ALLOWED_KEYS:
            raise ValueError("[flowsavvy] must be a table with known keys")
        values = dict(table)
        for key in ("enabled", "length_flow", "busy_calendar"):
            if type(values.get(key, False)) is not bool:
                raise ValueError(f"[flowsavvy] {key} must be a boolean")
        for key in ("check_seconds", "max_age_seconds"):
            if key in values and (type(values[key]) is not int or values[key] < 1):
                raise ValueError(f"[flowsavvy] {key} must be a positive integer")
        for key in ALLOWED_KEYS - {"enabled", "length_flow", "busy_calendar", "check_seconds", "max_age_seconds"}:
            if key not in values:
                continue
            value = values[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"[flowsavvy] {key} must be non-empty text")
            if key.endswith("_file"):
                path = Path(value)
                values[key] = (path if path.is_absolute() else base / path).resolve()
        settings = cls(**values)
        if settings.length_flow and not settings.enabled:
            raise ValueError("[flowsavvy] length_flow requires enabled")
        if settings.enabled:
            for key in ("state_file", "token_file", "list_id", "admin_hours_id", "deep_focus_hours_id", "errands_hours_id"):
                if getattr(settings, key) is None:
                    raise ValueError(f"[flowsavvy] enabled requires {key}")
            if settings.list_id == "inbox":
                raise ValueError("[flowsavvy] list_id cannot be inbox (inbox tasks are not scheduled)")
            if settings.max_age_seconds < settings.check_seconds:
                raise ValueError("[flowsavvy] max_age_seconds must cover check_seconds")
        return settings
