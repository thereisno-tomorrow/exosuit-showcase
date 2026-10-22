"""`[notion_view]` parsing (step H spec §5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gremlin_town.notion_view.settings import NotionViewSettings, NotionViewSettingsError

BASE = Path("/etc/gremlin-town")


def test_absent_means_off():
    assert NotionViewSettings.from_table(None, base=BASE) == NotionViewSettings(enabled=False)


def test_a_full_table():
    settings = NotionViewSettings.from_table(
        {
            "enabled": True, "token_file": "/etc/gremlin-town/notion-token", "database_id": " abc ",
            "state_file": "state/notion-view.sqlite3", "check_seconds": 30, "repair_hours": 12, "done_days": 7,
        },
        base=BASE,
    )
    assert settings.enabled and settings.database_id == "abc"
    assert settings.state_file == (BASE / "state/notion-view.sqlite3").resolve()
    assert (settings.check_seconds, settings.repair_hours, settings.done_days) == (30, 12, 7)


def test_defaults():
    settings = NotionViewSettings.from_table({"enabled": True, "state_file": "/x.sqlite3"}, base=BASE)
    assert (settings.check_seconds, settings.repair_hours, settings.done_days) == (60, 24, 14)
    assert settings.token_file is None and settings.database_id is None


@pytest.mark.parametrize(
    "table, message",
    [
        ({"enabled": True}, "requires state_file"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"surprise": 1}, "unknown keys"),
        ({"check_seconds": 0}, "check_seconds must be a positive integer"),
        ({"done_days": True}, "done_days must be a positive integer"),
        ({"database_id": ""}, "database_id must be a non-empty string"),
        ({"token_file": ""}, "token_file must be a non-empty string"),
    ],
)
def test_bad_tables_fail_loudly(table, message):
    with pytest.raises(NotionViewSettingsError, match=message):
        NotionViewSettings.from_table(table, base=BASE)
