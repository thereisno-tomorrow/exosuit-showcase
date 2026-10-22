"""MailReader: headers plus snippet, said_by from From, and no message body ever fetched."""

import json
from pathlib import Path

import httpx
import pytest

from gremlin_town.sources.google.mail import MailReader
from gremlin_town.sources.google.settings import GoogleSettings
from gremlin_town.sources.result import ReadStatus, UnavailableReason

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "google"
ACCESS_TOKEN = "test-access-token"


@pytest.mark.parametrize("labels,unread", [(["INBOX", "UNREAD"], True), (["INBOX"], False), ([], False)])
def test_unread_check_fetches_only_labels_with_get(labels, unread):
    made, calls = reader(lambda request: httpx.Response(200, json={"id": "fake1", "labelIds": labels}))
    assert made.is_unread("fake1") is unread
    request, = calls
    assert request.method == "GET"
    assert request.url.path.endswith("/messages/fake1")
    assert dict(request.url.params) == {"format": "minimal", "fields": "id,labelIds"}
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize("body", [{"id": "fake1"}, {"id": "wrong", "labelIds": []},
                                  {"id": "fake1", "labelIds": [1]}])
def test_unread_check_rejects_missing_or_invalid_labels(body):
    from gremlin_town.sources.google.auth import GoogleAuthError
    made, _ = reader(lambda request: httpx.Response(200, json=body))
    with pytest.raises(GoogleAuthError):
        made.is_unread("fake1")


def test_unread_check_permission_failure_is_never_read_mail():
    from gremlin_town.sources.google.auth import GoogleAuthError
    made, _ = reader(lambda request: httpx.Response(403, json={"error": "fixture"}))
    with pytest.raises(GoogleAuthError) as error:
        made.is_unread("fake1")
    assert error.value.reason == UnavailableReason.PERMISSION_DENIED


class FixedToken:
    def token(self) -> str:
        return ACCESS_TOKEN


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def settings(*, account="nic@example.test"):
    return GoogleSettings.from_table({
        "enabled": True, "account": account, "credential_env": "X", "calendars": ["primary"],
    })


def recording_transport(handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), calls


def reader(handler) -> tuple[MailReader, list[httpx.Request]]:
    transport, calls = recording_transport(handler)
    return MailReader(settings(), FixedToken(), transport=transport), calls


def default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/gmail/v1/users/me/messages":
        return httpx.Response(200, json=load_fixture("gmail_message_list.json"))
    if path == "/gmail/v1/users/me/messages/msg-1":
        return httpx.Response(200, json=load_fixture("gmail_message_metadata_1.json"))
    if path == "/gmail/v1/users/me/messages/msg-2":
        return httpx.Response(200, json=load_fixture("gmail_message_metadata_2.json"))
    raise AssertionError(f"unexpected request: {request.url}")


def test_a_message_becomes_an_item_with_said_by_and_headers():
    made, calls = reader(default_handler)
    read = made.messages("is:unread", 10)

    assert read.status is ReadStatus.OK
    assert len(read.items) == 2
    first = read.items[0]
    assert first.source == "google.mail"
    assert first.account == "nic@example.test"
    assert first.provider_id == "msg-1"
    assert first.said_by == "Sam <sam@example.test>"
    assert first.fields["subject"] == "Deck for the board"
    assert first.fields["snippet"] == "Can you send the deck by Friday?"
    assert first.fields["to"] == "Nic <nic@example.test>"
    assert first.fields["date"] == "Fri, 25 Sep 2026 09:00:00 +0800"

    list_request = calls[0]
    assert dict(httpx.QueryParams(list_request.url.query))["q"] == "is:unread"


def test_no_kind_is_assigned_to_an_email_item():
    made, _ = reader(default_handler)
    read = made.messages("is:unread", 10)
    for item in read.items:
        assert not hasattr(item, "kind")
        assert "kind" not in item.fields


def test_metadata_format_is_requested_and_no_body_is_fetched():
    made, calls = reader(default_handler)
    made.messages("is:unread", 10)
    detail_calls = [call for call in calls if "/messages/msg-" in call.url.path]
    assert len(detail_calls) == 2
    for call in detail_calls:
        params = dict(httpx.QueryParams(call.url.query))
        assert params["format"] == "metadata"
        assert set(httpx.QueryParams(call.url.query).get_list("metadataHeaders")) == {
            "From", "To", "Subject", "Date",
        }


def test_one_failed_message_fetch_makes_the_read_partial_and_names_it():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return httpx.Response(500, json={"error": "boom"})
        return default_handler(request)

    made, _ = reader(handler)
    read = made.messages("is:unread", 10)
    assert read.status is ReadStatus.PARTIAL
    assert read.unavailable == ("msg-1",)
    assert [item.provider_id for item in read.items] == ["msg-2"]


@pytest.mark.parametrize(("status", "reason"), [
    (403, UnavailableReason.PERMISSION_DENIED),
    (429, UnavailableReason.RATE_LIMITED),
    (500, UnavailableReason.PROVIDER_ERROR),
])
def test_a_failed_list_call_makes_the_whole_read_unavailable(status, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "no"})

    made, _ = reader(handler)
    read = made.messages("is:unread", 10)
    assert read.status is ReadStatus.UNAVAILABLE
    assert read.reason is reason


def test_disabled_settings_never_touch_the_token_provider_or_transport():
    class ExplodingToken:
        def token(self) -> str:
            raise AssertionError("must not be called while disabled")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send a request while disabled")

    transport = httpx.MockTransport(handler)
    disabled = GoogleSettings.from_table(None)
    made = MailReader(disabled, ExplodingToken(), transport=transport)
    read = made.messages("is:unread", 10)
    assert read.status is ReadStatus.UNAVAILABLE
    assert read.reason is UnavailableReason.DISABLED
