"""Google source settings: off by default, unknown keys rejected."""

from datetime import UTC, datetime

import httpx
import pytest

from gremlin_town.sources.google.calendar import CalendarReader
from gremlin_town.sources.google.mail import MailReader
from gremlin_town.sources.google.settings import GoogleSettings, GoogleSettingsError
from gremlin_town.sources.result import ReadStatus, UnavailableReason


class _UnusedTokenProvider:
    """Fails the test if a disabled reader ever asks for a token."""

    def token(self) -> str:
        raise AssertionError("a disabled reader must never request a token")


def test_missing_table_is_disabled_with_default_calendar():
    settings = GoogleSettings.from_table(None)
    assert settings.enabled is False
    assert settings.account is None
    assert settings.credential_env is None
    assert settings.calendars == ("primary",)


def test_empty_table_is_disabled():
    settings = GoogleSettings.from_table({})
    assert settings.enabled is False


def test_enabled_table_reads_configured_fields():
    settings = GoogleSettings.from_table({
        "enabled": True,
        "account": "nic@example.test",
        "credential_env": "GOOGLE_CREDENTIAL_PATH",
        "calendars": ["primary", "team@example.test"],
    })
    assert settings.enabled is True
    assert settings.account == "nic@example.test"
    assert settings.credential_env == "GOOGLE_CREDENTIAL_PATH"
    assert settings.calendars == ("primary", "team@example.test")


def test_unknown_key_is_rejected():
    with pytest.raises(GoogleSettingsError):
        GoogleSettings.from_table({"enabled": False, "sync_drive": True})


@pytest.mark.parametrize("table", [
    {"enabled": True},
    {"enabled": True, "account": "nic@example.test"},
    {"enabled": True, "credential_env": "GOOGLE_CREDENTIAL_PATH"},
])
def test_enabled_requires_account_and_credential_env(table):
    with pytest.raises(GoogleSettingsError):
        GoogleSettings.from_table(table)


@pytest.mark.parametrize("table", [
    {"enabled": "yes"},
    {"enabled": True, "account": "", "credential_env": "X"},
    {"enabled": True, "account": "nic@example.test", "credential_env": ""},
    {"calendars": []},
    {"calendars": ["primary", ""]},
    {"calendars": "primary"},
])
def test_malformed_values_are_rejected(table):
    with pytest.raises(GoogleSettingsError):
        GoogleSettings.from_table(table)


def test_not_a_table_is_rejected():
    with pytest.raises(GoogleSettingsError):
        GoogleSettings.from_table("not-a-table")


def test_disabled_readers_send_zero_requests():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no request should be sent while disabled")

    transport = httpx.MockTransport(handler)
    settings = GoogleSettings.from_table(None)
    token_provider = _UnusedTokenProvider()

    calendar_read = CalendarReader(settings, token_provider, transport=transport).events(
        datetime(2026, 9, 26, tzinfo=UTC), datetime(2026, 9, 27, tzinfo=UTC),
    )
    mail_read = MailReader(settings, token_provider, transport=transport).messages("is:unread", 10)

    for read in (calendar_read, mail_read):
        assert read.status is ReadStatus.UNAVAILABLE
        assert read.reason is UnavailableReason.DISABLED
        assert read.items == ()
    assert calls == []
