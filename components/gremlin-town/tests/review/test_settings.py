"""`[review]`/`[orientation]` settings: off by default, unknown keys rejected.

(step E spec §8; `[orientation]` per step F spec §2, §8.)
"""

from __future__ import annotations

import pytest

from gremlin_town.review.settings import (
    DEFAULT_DELIVERY_PARTICIPANT,
    DEFAULT_SENDER_PARTICIPANT,
    OrientationSettings,
    OrientationSettingsError,
    ReviewSettings,
    ReviewSettingsError,
)


def test_missing_table_is_disabled_with_defaults() -> None:
    settings = ReviewSettings.from_table(None)
    assert settings.enabled is False
    assert settings.sender_participant == DEFAULT_SENDER_PARTICIPANT
    assert settings.delivery_participant == DEFAULT_DELIVERY_PARTICIPANT
    assert settings.why_log_file is None
    assert settings.gap_minutes == 30
    assert (settings.gap_start_hour, settings.gap_end_hour) == (8, 21)


def test_empty_table_is_disabled() -> None:
    assert ReviewSettings.from_table({}).enabled is False


def test_opening_deadlines_defaults_off_and_is_known_to_backup_validator():
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not ReviewSettings.from_table(None).opening_deadlines
    assert ReviewSettings.from_table({"opening_deadlines": True}).opening_deadlines
    assert "opening_deadlines" in RUNTIME_CONFIG_SCHEMA["review"]
    with pytest.raises(ReviewSettingsError, match="opening_deadlines"):
        ReviewSettings.from_table({"opening_deadlines": "yes"})


def test_important_mail_defaults_off_and_is_known_to_backup_validator():
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert not ReviewSettings.from_table(None).important_mail
    assert not ReviewSettings.from_table({"enabled": True, "why_log_file": "fixture.sqlite3"}).important_mail
    assert {"important_mail", "important_mail_log", "important_mail_deadline_seconds"} <= RUNTIME_CONFIG_SCHEMA["review"]
    assert ReviewSettings.from_table(None).important_mail_deadline_seconds == 10


@pytest.mark.parametrize("value", [True, 0, -1, "10", float("inf"), float("nan")])
def test_important_mail_deadline_rejects_invalid_values(value):
    with pytest.raises(ReviewSettingsError, match="important_mail_deadline_seconds"):
        ReviewSettings.from_table(dict(important_mail_deadline_seconds=value))


@pytest.mark.parametrize("changes", [dict(important_mail="yes"), dict(important_mail=True),
                                    dict(important_mail=True, important_mail_log=""),
                                    dict(important_mail_log=123), dict(important_mail_log=" ")])
def test_important_mail_configuration_refusals(changes):
    with pytest.raises(ReviewSettingsError, match="important_mail"):
        ReviewSettings.from_table(dict(enabled=True, why_log_file="fixture.sqlite3", **changes))


def test_important_mail_requires_enabled_review_and_path():
    with pytest.raises(ReviewSettingsError, match="important_mail requires"):
        ReviewSettings.from_table(dict(important_mail=True, important_mail_log="flagged-mail.json"))
    settings = ReviewSettings.from_table(dict(enabled=True, why_log_file="fixture.sqlite3",
        important_mail=True, important_mail_log="flagged-mail.json"))
    assert settings.important_mail and settings.important_mail_log == "flagged-mail.json"


def test_enabled_table_reads_configured_fields() -> None:
    settings = ReviewSettings.from_table({
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": "/var/lib/gremlin-town-brain/why/why.sqlite3",
        "gap_minutes": 45,
        "gap_hours": "07:00-22:00",
    })
    assert settings.enabled is True
    assert settings.project == "vault_home"
    assert settings.why_log_file == "/var/lib/gremlin-town-brain/why/why.sqlite3"
    assert settings.gap_minutes == 45
    assert (settings.gap_start_hour, settings.gap_end_hour) == (7, 22)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table({"enabled": False, "notify_slack": True})


def test_enabled_requires_why_log_file() -> None:
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table({"enabled": True})


@pytest.mark.parametrize("table", [
    {"enabled": "yes"},
    {"sender_participant": ""},
    {"delivery_participant": ""},
    {"project": ""},
    {"why_log_file": ""},
    {"gap_minutes": 0},
    {"gap_minutes": -5},
    {"gap_minutes": True},
    {"gap_hours": "8:00-21:00"},
    {"gap_hours": "08:30-21:00"},
    {"gap_hours": "21:00-08:00"},
    {"gap_hours": "not-a-range"},
])
def test_malformed_values_are_rejected(table: dict[str, object]) -> None:
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table(table)


def test_not_a_table_is_rejected() -> None:
    with pytest.raises(ReviewSettingsError):
        ReviewSettings.from_table("not-a-table")  # type: ignore[arg-type]


# --- [orientation] ---------------------------------------------------------------------------


def test_orientation_missing_table_is_disabled() -> None:
    settings = OrientationSettings.from_table(None, review_enabled=True)
    assert settings.enabled is False


def test_orientation_empty_table_is_disabled() -> None:
    settings = OrientationSettings.from_table({}, review_enabled=True)
    assert settings.enabled is False


def test_orientation_enabled_with_review_enabled() -> None:
    settings = OrientationSettings.from_table({"enabled": True}, review_enabled=True)
    assert settings.enabled is True


def test_orientation_enabled_requires_review_enabled() -> None:
    with pytest.raises(OrientationSettingsError):
        OrientationSettings.from_table({"enabled": True}, review_enabled=False)


def test_orientation_disabled_does_not_require_review_enabled() -> None:
    settings = OrientationSettings.from_table({"enabled": False}, review_enabled=False)
    assert settings.enabled is False
    settings = OrientationSettings.from_table(None, review_enabled=False)
    assert settings.enabled is False


def test_orientation_unknown_key_is_rejected() -> None:
    with pytest.raises(OrientationSettingsError):
        OrientationSettings.from_table({"enabled": False, "gap_minutes": 30}, review_enabled=True)


def test_orientation_enabled_must_be_a_boolean() -> None:
    with pytest.raises(OrientationSettingsError):
        OrientationSettings.from_table({"enabled": "yes"}, review_enabled=True)


def test_orientation_not_a_table_is_rejected() -> None:
    with pytest.raises(OrientationSettingsError):
        OrientationSettings.from_table("not-a-table", review_enabled=True)  # type: ignore[arg-type]


def test_orientation_ranks_by_points_unless_set_to_the_nine_rules() -> None:
    """PRD §5 (Accepted - Nic, 28 Sep 2026); the nine rules stay one setting away until a week of
    real use ("Yes, keep a way back")."""

    assert OrientationSettings.from_table(None, review_enabled=True).ranking == "points"
    assert OrientationSettings.from_table({"enabled": True}, review_enabled=True).ranking == "points"
    assert OrientationSettings.from_table({"enabled": True, "ranking": "rules"}, review_enabled=True).ranking == "rules"
    for bad in ("urgency", "", 1, None):
        with pytest.raises(OrientationSettingsError, match="ranking must be one of"):
            OrientationSettings.from_table({"enabled": True, "ranking": bad}, review_enabled=True)


def test_time_blocks_are_off_until_switched_on() -> None:
    """PRD §4.3: they read the calendar, so they wait for Nic's Google consent (ROA-18)."""

    assert OrientationSettings.from_table({"enabled": True}, review_enabled=True).blocks is False
    assert OrientationSettings.from_table({"enabled": True, "blocks": True}, review_enabled=True).blocks is True
    with pytest.raises(OrientationSettingsError, match="blocks must be a boolean"):
        OrientationSettings.from_table({"enabled": True, "blocks": "yes"}, review_enabled=True)
