"""Google credential loading and token refresh: exactly calendar entries plus read-only Gmail
(step D spec §3.5, changed by N8 on 28 Sep 2026), enforced before any request."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gremlin_town.sources.google.auth import (
    ALLOWED_SCOPES,
    CALENDAR_EVENTS_SCOPE,
    GMAIL_READ_ONLY_SCOPE,
    GoogleAuthError,
    GoogleCredential,
    GoogleTokenProvider,
)
from gremlin_town.sources.result import UnavailableReason


NOW = datetime(2026, 9, 26, 3, tzinfo=UTC)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# The pair step D allowed before N8: still refused, so a stale consent fails loudly.
OLD_READ_ONLY_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def credential_json(*, scopes=None, **overrides):
    data = {
        "client_id": "client-123",
        "client_secret": "secret-abc",
        "refresh_token": "refresh-xyz",
        "scopes": list(scopes) if scopes is not None else [CALENDAR_SCOPE, GMAIL_SCOPE],
        "account": "nic@example.test",
    }
    data.update(overrides)
    return data


def write_credential(tmp_path, **overrides):
    import json

    path = tmp_path / "google-credential.json"
    path.write_text(json.dumps(credential_json(**overrides)), encoding="utf-8")
    return path


def credential(**overrides) -> GoogleCredential:
    data = credential_json(**overrides)
    return GoogleCredential(
        client_id=data["client_id"], client_secret=data["client_secret"],
        refresh_token=data["refresh_token"], scopes=frozenset(data["scopes"]), account=data["account"],
    )


def recording_transport(*responses):
    calls: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert remaining, "unexpected token request"
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


def token_response(token="access-1", expires_in=3600):
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


def invalid_grant():
    return httpx.Response(400, json={"error": "invalid_grant"})


# --- GoogleCredential.load -------------------------------------------------

def test_the_allowed_pair_is_calendar_entries_and_read_only_gmail():
    assert CALENDAR_EVENTS_SCOPE == CALENDAR_SCOPE
    assert GMAIL_READ_ONLY_SCOPE == GMAIL_SCOPE
    assert ALLOWED_SCOPES == frozenset({CALENDAR_SCOPE, GMAIL_SCOPE})


def test_load_accepts_exactly_calendar_entries_and_read_only_gmail(tmp_path):
    path = write_credential(tmp_path)
    loaded = GoogleCredential.load(path)
    assert loaded.scopes == ALLOWED_SCOPES
    assert loaded.account == "nic@example.test"
    assert loaded.writes_calendar is True


@pytest.mark.parametrize("scopes", [
    [CALENDAR_SCOPE],
    [GMAIL_SCOPE],
    [OLD_READ_ONLY_CALENDAR_SCOPE, GMAIL_SCOPE],
    [CALENDAR_SCOPE, OLD_READ_ONLY_CALENDAR_SCOPE, GMAIL_SCOPE],
    [CALENDAR_SCOPE, GMAIL_SCOPE, "https://www.googleapis.com/auth/calendar"],
    ["https://www.googleapis.com/auth/calendar", GMAIL_SCOPE],
    [CALENDAR_SCOPE, "https://www.googleapis.com/auth/gmail.modify"],
    [CALENDAR_SCOPE, GMAIL_SCOPE, "https://www.googleapis.com/auth/gmail.send"],
    [],
])
def test_load_refuses_anything_but_the_allowed_pair(tmp_path, scopes):
    path = write_credential(tmp_path, scopes=scopes)
    with pytest.raises(GoogleAuthError) as caught:
        GoogleCredential.load(path)
    assert caught.value.reason is UnavailableReason.SCOPE_NOT_READ_ONLY


def test_a_refused_credential_is_refused_before_any_request(tmp_path):
    """No token exchange (or any other request) can happen: loading fails first."""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no request may be sent for a refused credential")

    path = write_credential(tmp_path, scopes=[OLD_READ_ONLY_CALENDAR_SCOPE, GMAIL_SCOPE])
    with pytest.raises(GoogleAuthError):
        GoogleTokenProvider(GoogleCredential.load(path), transport=httpx.MockTransport(handler))
    assert calls == []


def test_writes_calendar_is_read_from_the_scopes():
    assert credential().writes_calendar is True
    assert credential(scopes=[GMAIL_SCOPE]).writes_calendar is False


def test_load_missing_file_is_credential_missing(tmp_path):
    with pytest.raises(GoogleAuthError) as caught:
        GoogleCredential.load(tmp_path / "does-not-exist.json")
    assert caught.value.reason is UnavailableReason.CREDENTIAL_MISSING


@pytest.mark.parametrize("write", [
    lambda p: p.write_text("not json", encoding="utf-8"),
    lambda p: p.write_text("[]", encoding="utf-8"),
    lambda p: p.write_text('{"client_id": "x"}', encoding="utf-8"),
    lambda p: p.write_text('{"client_id": "", "client_secret": "s", "refresh_token": "r", '
                           '"account": "a", "scopes": []}', encoding="utf-8"),
])
def test_load_malformed_file_is_credential_invalid(tmp_path, write):
    path = tmp_path / "google-credential.json"
    write(path)
    with pytest.raises(GoogleAuthError) as caught:
        GoogleCredential.load(path)
    assert caught.value.reason is UnavailableReason.CREDENTIAL_INVALID


# --- GoogleTokenProvider -----------------------------------------------------

def test_token_is_minted_from_the_refresh_token():
    transport, calls = recording_transport(token_response("access-1"))
    provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock())
    assert provider.token() == "access-1"
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    body = request.content.decode()
    assert "refresh_token=refresh-xyz" in body
    assert "grant_type=refresh_token" in body


def test_token_is_cached_until_60_seconds_before_expiry():
    clock = Clock()
    transport, calls = recording_transport(
        token_response("access-1", expires_in=3600), token_response("access-2", expires_in=3600),
    )
    provider = GoogleTokenProvider(credential(), transport=transport, clock=clock)
    assert provider.token() == "access-1"
    clock.now = NOW + timedelta(seconds=3600 - 61)
    assert provider.token() == "access-1"
    assert len(calls) == 1
    clock.now = NOW + timedelta(seconds=3600 - 59)
    assert provider.token() == "access-2"
    assert len(calls) == 2


def test_invalid_grant_means_token_revoked():
    transport, _ = recording_transport(invalid_grant())
    provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock())
    with pytest.raises(GoogleAuthError) as caught:
        provider.token()
    assert caught.value.reason is UnavailableReason.TOKEN_REVOKED


@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(429, json={"error": "rate_limit_exceeded"}), UnavailableReason.RATE_LIMITED),
    (httpx.Response(500, json={"error": "backend_error"}), UnavailableReason.PROVIDER_ERROR),
    (httpx.Response(503, content=b"unavailable"), UnavailableReason.PROVIDER_ERROR),
    (httpx.Response(400, json={"error": "invalid_client"}), UnavailableReason.CREDENTIAL_INVALID),
])
def test_refresh_failures_are_classified(response, reason):
    transport, _ = recording_transport(response)
    provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock())
    with pytest.raises(GoogleAuthError) as caught:
        provider.token()
    assert caught.value.reason is reason


def test_network_failure_is_a_network_error():
    transport, _ = recording_transport(httpx.ConnectError("refused"))
    provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock())
    with pytest.raises(GoogleAuthError) as caught:
        provider.token()
    assert caught.value.reason is UnavailableReason.NETWORK_ERROR


def test_malformed_token_response_is_a_provider_error():
    transport, _ = recording_transport(httpx.Response(200, content=b"not json"))
    provider = GoogleTokenProvider(credential(), transport=transport, clock=Clock())
    with pytest.raises(GoogleAuthError) as caught:
        provider.token()
    assert caught.value.reason is UnavailableReason.PROVIDER_ERROR
