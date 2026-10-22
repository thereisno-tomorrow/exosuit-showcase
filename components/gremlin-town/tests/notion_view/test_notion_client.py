"""The Notion calls, against a fake transport: headers, pacing, retries and refusals (step H spec §4)."""

from __future__ import annotations

import json

import pytest
from brain_core.github_storage import GitHubTransportError, HTTPResponse

from gremlin_town.notion_view.client import NOTION_VERSION, NotionClient, NotionError


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body):
        self.requests.append((method, url, dict(headers), None if body is None else json.loads(body)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok(value, status=200):
    return HTTPResponse(status, {}, json.dumps(value).encode())


def client(responses, sleeps=None):
    clock = iter(float(n) for n in range(1000))
    return NotionClient(
        "secret-key", transport=FakeTransport(responses), sleep=(sleeps.append if sleeps is not None else lambda _: None),
        clock=lambda: next(clock),
    )


def test_calls_carry_the_key_and_version():
    notion = client([ok({"id": "page-1"})])
    assert notion.create_page("db", {"Name": {"title": []}}) == "page-1"
    method, url, headers, body = notion._transport.requests[0]
    assert (method, url) == ("POST", "https://api.notion.com/v1/pages")
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["Notion-Version"] == NOTION_VERSION
    assert body == {"parent": {"database_id": "db"}, "properties": {"Name": {"title": []}}}


def test_a_429_waits_for_retry_after_then_succeeds():
    sleeps: list[float] = []
    notion = client([HTTPResponse(429, {"Retry-After": "2"}, b"{}"), ok({})], sleeps)
    notion.update_page("page-1", {})
    assert 2.0 in sleeps


def test_a_server_error_is_retried_for_updates_but_not_for_creates():
    notion = client([HTTPResponse(502, {}, b""), ok({})])
    notion.update_page("page-1", {})
    creator = client([HTTPResponse(502, {}, b""), ok({"id": "page-2"})])
    with pytest.raises(NotionError) as error:
        creator.create_page("db", {})
    assert error.value.status == 502
    assert len(creator._transport.requests) == 1


def test_no_answer_on_a_create_is_not_retried():
    creator = client([GitHubTransportError("github_transport_unavailable", "down"), ok({"id": "page-2"})])
    with pytest.raises(NotionError):
        creator.create_page("db", {})
    assert len(creator._transport.requests) == 1


def test_retries_are_bounded():
    notion = client([HTTPResponse(429, {}, b"{}")] * 3)
    with pytest.raises(NotionError, match="kept refusing"):
        notion.update_page("page-1", {})


def test_a_refusal_carries_notions_code_and_message():
    notion = client([ok({"code": "validation_error", "message": "Status is not a property"}, status=400)])
    with pytest.raises(NotionError, match="validation_error Status is not a property") as error:
        notion.update_page("page-1", {})
    assert error.value.status == 400 and error.value.code == "validation_error"


def test_archiving_a_page_already_gone_is_fine():
    client([ok({"code": "object_not_found", "message": "gone"}, status=404)]).archive_page("page-1")


def test_pages_follow_notions_cursor():
    notion = client([
        ok({"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur"}),
        ok({"results": [{"id": "b"}], "has_more": False}),
    ])
    assert [page["id"] for page in notion.pages("db")] == ["a", "b"]
    assert notion._transport.requests[1][3] == {"page_size": 100, "start_cursor": "cur"}


def test_calls_are_paced():
    sleeps: list[float] = []
    clock = iter([0.0, 0.1, 0.1])  # first call at 0.0; the second asks at 0.1, so it waits 0.24
    notion = NotionClient(
        "secret-key", transport=FakeTransport([ok({}), ok({})]), sleep=sleeps.append, clock=lambda: next(clock),
    )
    notion.update_page("a", {})
    notion.update_page("b", {})
    assert sleeps == [pytest.approx(0.24)]


def test_the_key_must_be_clean():
    with pytest.raises(ValueError):
        NotionClient(" key\n")
