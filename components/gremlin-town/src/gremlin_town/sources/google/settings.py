"""Configuration for the Google source readers: off unless explicitly enabled.

Mirrors ``runtime.py``'s TOML-table style (reject unknown keys, explicit
defaults) without importing its private helpers, so this module has no
dependency on the runtime wiring that hooks the readers in later (step E).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_KEYS = frozenset({"enabled", "account", "credential_env", "calendars"})
DEFAULT_CALENDARS = ("primary",)


class GoogleSettingsError(ValueError):
    """The ``[google]`` table is malformed."""


@dataclass(frozen=True, slots=True)
class GoogleSettings:
    enabled: bool
    account: str | None
    credential_env: str | None
    calendars: tuple[str, ...] = DEFAULT_CALENDARS

    @classmethod
    def from_table(cls, table: dict[str, Any] | None) -> "GoogleSettings":
        """Build settings from a parsed ``[google]`` TOML table, or its absence."""

        if table is None:
            return cls(enabled=False, account=None, credential_env=None)
        if not isinstance(table, dict):
            raise GoogleSettingsError("[google] must be a table")
        unknown = set(table) - ALLOWED_KEYS
        if unknown:
            raise GoogleSettingsError(f"[google] has unknown keys: {sorted(unknown)}")

        enabled = table.get("enabled", False)
        if not isinstance(enabled, bool):
            raise GoogleSettingsError("[google] enabled must be a boolean")

        account = table.get("account")
        if account is not None and (not isinstance(account, str) or not account):
            raise GoogleSettingsError("[google] account must be a non-empty string")

        credential_env = table.get("credential_env")
        if credential_env is not None and (not isinstance(credential_env, str) or not credential_env):
            raise GoogleSettingsError("[google] credential_env must be a non-empty string")

        calendars_value = table.get("calendars", list(DEFAULT_CALENDARS))
        if (not isinstance(calendars_value, list) or not calendars_value
                or not all(isinstance(item, str) and item for item in calendars_value)):
            raise GoogleSettingsError("[google] calendars must be a non-empty list of strings")

        if enabled and (account is None or credential_env is None):
            raise GoogleSettingsError("[google] enabled requires account and credential_env")

        return cls(enabled=enabled, account=account, credential_env=credential_env,
                  calendars=tuple(calendars_value))
