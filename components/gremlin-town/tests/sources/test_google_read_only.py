"""Cross-cutting read-only proof: every reader request is GET, the token exchange is
the one POST, and no module under gremlin_town.sources imports from archive/."""

import ast
from pathlib import Path

import httpx

from gremlin_town.sources.google.auth import GoogleTokenProvider
from gremlin_town.sources.google.calendar import CalendarReader
from gremlin_town.sources.google.mail import MailReader
from gremlin_town.sources.google.settings import GoogleSettings
from gremlin_town.sources.result import ReadStatus

from test_google_auth import NOW, Clock, credential, token_response
from test_google_calendar import TIME_MAX, TIME_MIN
from test_google_mail import default_handler as mail_handler
from test_google_calendar import load_fixture as calendar_fixture

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "src" / "gremlin_town" / "sources"


def shared_settings() -> GoogleSettings:
    return GoogleSettings.from_table({
        "enabled": True, "account": "nic@example.test", "credential_env": "X", "calendars": ["primary"],
    })


def test_every_recorded_request_is_a_read_apart_from_the_one_token_post():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return token_response()
        if request.url.host == "www.googleapis.com":
            # No nextPageToken here, so one request per calendar is enough.
            return httpx.Response(200, json=calendar_fixture("calendar_events_page2.json"))
        if request.url.host == "gmail.googleapis.com":
            return mail_handler(request)
        raise AssertionError(f"unexpected host: {request.url.host}")

    transport = httpx.MockTransport(handler)
    settings = shared_settings()
    token_provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock(NOW))

    calendar_read = CalendarReader(settings, token_provider, transport=transport).events(TIME_MIN, TIME_MAX)
    mail_read = MailReader(settings, token_provider, transport=transport).messages("is:unread", 10)

    assert calendar_read.status is ReadStatus.OK
    assert mail_read.status is ReadStatus.OK
    assert calls, "expected at least one recorded request"

    non_token = [call for call in calls if call.url.host != "oauth2.googleapis.com"]
    token_calls = [call for call in calls if call.url.host == "oauth2.googleapis.com"]

    assert all(call.method == "GET" for call in non_token)
    assert all(call.url.host == "www.googleapis.com" or call.url.host == "gmail.googleapis.com"
              for call in non_token)
    assert len(token_calls) == 1
    assert token_calls[0].method == "POST"


def test_no_sources_module_imports_or_names_archive():
    for path in SOURCES.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "archive" not in alias.name, f"{path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "archive" not in module, f"{path} imports from {module}"
        assert "archive" not in text, f"{path} names 'archive'"
