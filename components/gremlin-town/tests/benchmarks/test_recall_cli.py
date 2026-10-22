"""Unit tests for the live recall runner (gremlin_town.benchmarks.recall_cli).

Every call here goes through httpx.MockTransport, the same fake-transport
idiom tests/cli/test_cli.py and tests/cli/test_brain_client.py use for
JsonApiClient - this module must never touch a real hub, in this goal or any
other run of this suite.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gremlin_town.cli.client import JsonApiClient
from gremlin_town.benchmarks.recall_cli import build_parser, hub_search, main


QUESTIONS = {
    "_doc": "fixture for test_recall_cli.py",
    "questions": [
        {"id": "q1", "text": "what did I say about the printer ink", "expected_record_ids": ["brn_a"]},
        {"id": "q2", "text": "who is waiting on me", "expected_record_ids": ["brn_b"]},
    ],
}


def _write_questions(tmp_path) -> str:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
    return str(path)


def test_hub_search_calls_memory_search_with_the_tool_shaped_payload():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.method == "POST"
        assert request.url.path == "/v1/brain/memory.search"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"matches": [{"record_id": "brn_a"}, {"record_id": "brn_z"}]})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    search = hub_search(client, scope="personal")
    result = search("printer ink", 5)

    assert result == ("brn_a", "brn_z")
    assert calls == [{"query": "printer ink", "scope": "personal", "limit": 5}]


def test_hub_search_includes_project_id_only_when_scoped_to_a_project():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"matches": []})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    hub_search(client, scope="project", project_id="gtbc")("anything", 5)
    assert calls == [{"query": "anything", "scope": "project", "limit": 5, "project_id": "gtbc"}]


def test_cli_runs_one_memory_search_call_per_question_and_prints_the_report(tmp_path, capsys):
    questions_path = _write_questions(tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["query"] == "what did I say about the printer ink":
            return httpx.Response(200, json={"matches": [{"record_id": "brn_a"}]})
        return httpx.Response(200, json={"matches": [{"record_id": "brn_not_expected"}]})

    client = JsonApiClient("https://hub.invalid", "secret", transport=httpx.MockTransport(handler))
    exit_code = main(
        ["--base-url", "https://hub.invalid", "--questions", questions_path],
        client=client,
    )
    assert exit_code == 0
    assert len(calls) == 2  # exactly one memory.search call per question, no more

    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 2
    assert report["recall_at_1"] == 0.5
    assert report["recall_at_5"] == 0.5
    assert report["misses"] == ["q2"]


def test_cli_never_reaches_the_network_without_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GREMLIN_TOWN_TOKEN", raising=False)
    questions_path = _write_questions(tmp_path)
    exit_code = main(["--base-url", "https://hub.invalid", "--questions", questions_path])
    assert exit_code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "UNAUTHORIZED"


def test_cli_requires_project_id_when_scope_is_project(tmp_path, capsys):
    questions_path = _write_questions(tmp_path)
    exit_code = main([
        "--base-url", "https://hub.invalid", "--questions", questions_path,
        "--scope", "project", "--token", "secret",
    ])
    assert exit_code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "INVALID_INPUT"


def test_parser_requires_base_url_and_questions():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
