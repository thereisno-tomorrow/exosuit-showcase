"""Calendar entries through the real hub (N8; step E spec §5; ROA-66).

They switch on only with `[review]` enabled and a `[google]` credential carrying
`calendar.events`. Then a timed save adds an entry, a drop asks before removing it, and
the configured work door's HTTP `calendar.remove` removes it with Nic's words. MCP
never advertises calendar removal. Real loopback HTTP over the production
composition; GitHub and Google are fakes, and nothing reaches either.
"""

from __future__ import annotations

import json
from dataclasses import fields

import anyio
import pytest
from starlette.testclient import TestClient

from gremlin_town.mcp import brain_assembly
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import BrainOperation
from gremlin_town.runtime import ConfigurationError, build_runtime
from gremlin_town.sources.google import calendar_writer as calendar_writer_module
from gremlin_town.sources.google.calendar_writer import CalendarWriter
from gremlin_town.transport import FakeTransport
from tests.integration.test_brain_agent_http import post, running
from tests.integration.test_mcp_runtime import mcp_config
from tests.mcp.test_brain_composition import FIRST_TIME, LANE_B, _settings
from tests.mcp.test_github_brain_provider import _HistoryTransport
from tests.sources.fake_google_calendar import FakeGoogleCalendar, FixedToken

CREDENTIAL_ENV = "GT_TEST_GOOGLE_CREDENTIAL_FILE"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ_ONLY = "https://www.googleapis.com/auth/gmail.readonly"
OLD_CALENDAR_READ_ONLY = "https://www.googleapis.com/auth/calendar.events.readonly"


def _config(tmp_path, monkeypatch, *, review: bool, google: bool, scopes=(CALENDAR_EVENTS, GMAIL_READ_ONLY)):
    config, tokens = mcp_config(tmp_path)
    config.write_text(config.read_text().replace('client_id = "isolated-chatgpt-client"',
        '[[mcp.client]]\nparticipant = "chatgpt-web"\nname = "Fixture OAuth"\nclient_id = "isolated-chatgpt-client"'))
    settings = _settings(tmp_path)
    policy = json.loads(settings.policy_file.read_text())
    policy["subject_scopes"] = {"alice": ["bc:read", "bc:write"], "bob": ["bc:read"]}
    owner = policy["owners"][0]
    owner["readers"] = ["alice", "bob"]
    owner["writers"] = ["alice"]
    owner["operations"] = [op.value for op in BrainOperation if op != BrainOperation.CONTEXT_RETRIEVE]
    settings.policy_file.write_text(json.dumps(policy))
    credential = tmp_path / "google-credential.json"
    credential.write_text(json.dumps({
        "client_id": "client-123", "client_secret": "secret-abc", "refresh_token": "refresh-xyz",
        "scopes": list(scopes), "account": "nic@example.test",
    }), encoding="utf-8")
    monkeypatch.setenv(CREDENTIAL_ENV, str(credential))
    with config.open("a", encoding="utf-8") as handle:
        handle.write('\n[work]\ndoor_participant = "alice"\n')
        handle.write("\n[brain_core]\nenabled = true\n")
        for field in fields(settings):
            value = getattr(settings, field.name)
            if value is not None:
                encoded = json.dumps(value) if isinstance(value, bool) else json.dumps(str(value).replace(chr(92), '/'))
                handle.write(f"{field.name} = {encoded}\n")
        if review:
            why_log = (tmp_path / "why.sqlite3").as_posix()
            handle.write(f'\n[review]\nenabled = true\nproject = "demo"\nwhy_log_file = "{why_log}"\n')
        if google:
            handle.write(
                f'\n[google]\nenabled = true\naccount = "nic@example.test"\ncredential_env = "{CREDENTIAL_ENV}"\n'
            )
    return config, tokens


@pytest.fixture
def fake_google(monkeypatch):
    """The runtime's own calendar writer, with its token and HTTP replaced by fakes."""

    google = FakeGoogleCalendar()
    real = calendar_writer_module.CalendarWriter
    monkeypatch.setattr(
        calendar_writer_module, "CalendarWriter",
        lambda settings, _token_provider: real(settings, FixedToken(), transport=google.transport()),
    )
    return google


def _hub(tmp_path, monkeypatch, **options):
    config, tokens = _config(tmp_path, monkeypatch, **options)
    remote = LANE_B.FakeGitHub()
    monkeypatch.setattr(
        brain_assembly, "build_brain_runtime",
        lambda configured, **kwargs: build_brain_runtime(
            configured, github_transport=_HistoryTransport(remote), operation_clock=lambda: FIRST_TIME, **kwargs,
        ),
    )
    return build_runtime(config, provider=FakeTransport(), start_worker=False), tokens, remote


SAVE = {
    "operation_id": "calendar-save-1", "scope": "personal", "kind": "item", "level": "accepted",
    "said_by": "owner", "came_by": "direct", "words": "Dentist Friday 7pm", "title": "Dentist",
    "body": "Dentist Friday 7pm", "time_words": "Friday 7pm",
}


def _drop(saved):
    return {
        "operation_id": "calendar-drop-1", "scope": "personal", "record_id": saved["record_id"],
        "expected_sha256": saved["content_sha256"], "said_by": "owner", "came_by": "direct",
        "words": "Drop the dentist, not going", "state": "dropped", "reason": "Not going",
    }


def _remove(dropped, **overrides):
    body = {
        "operation_id": "calendar-remove-1", "record_id": dropped["record_id"],
        "said_by": "owner", "came_by": "direct", "words": "yes",
    }
    body.update(overrides)
    return body


def test_on_a_timed_save_adds_the_entry_a_drop_asks_and_only_nics_yes_removes_it(tmp_path, monkeypatch, fake_google):
    runtime, tokens, remote = _hub(tmp_path, monkeypatch, review=True, google=True)
    try:
        assert isinstance(runtime.calendar_writer, CalendarWriter)
        assert runtime.review_sources.calendar_adds_timed is True
        with running(runtime) as client:
            saved = post(client, tokens["alice"], "memory.save", SAVE)
            assert saved.status_code == 200, saved.text
            saved = saved.json()
            assert saved["calendar"]["status"] == "added"
            assert saved["calendar"]["text"] == (
                "Added to your calendar, Fri 25 Sep 7 pm, 30 min, under your calendar limit"
            )
            assert "calendar_suggestion" not in saved

            before = remote.branch
            before_google = list(fake_google.requests())
            retired_change = _drop(saved)
            invalid = post(client, tokens["alice"], "memory.change", retired_change)
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["error"]["code"] == "brain.invalid_argument"
            assert invalid.json()["error"]["field"] == "state"
            assert invalid.json()["error"]["fix"]
            assert remote.branch == before
            assert fake_google.requests() == before_google

            item_change = {key: value for key, value in retired_change.items() if key != "state"}
            refused_change = post(client, tokens["alice"], "memory.change", item_change)
            assert refused_change.status_code == 403, refused_change.text
            assert refused_change.json()["error"]["code"] == "ACCESS_DENIED"
            assert refused_change.json()["error"]["fix"] == "use bc_work"
            assert remote.branch == before
            assert fake_google.requests() == before_google

            end = {
                "operation_id": "calendar-drop-1", "message": "Drop the dentist, not going",
                "said_by": "owner", "came_by": "direct", "moves": [{
                    "move": "end", "thing": saved["record_id"], "ending": "dropped",
                    "reason": "Not going", "words": "Drop the dentist, not going",
                }],
            }
            changed = post(client, tokens["alice"], "work", end)
            assert changed.status_code == 200, changed.text
            changed = changed.json()
            assert changed["status"] == "accepted"
            assert changed["calendar"]["status"] == "ask_remove"
            assert changed["calendar"]["question"] == "Remove it from your calendar?"
            [dropped] = changed["things"]
            assert len(fake_google.live()) == 1
            before = remote.branch
            repeated = post(client, tokens["alice"], "work", end)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json() == {**changed, "status": "duplicate"}
            assert remote.branch == before
            assert len(fake_google.live()) == 1

            # A reader cannot remove it, and neither can an agent's own words.
            reader = post(client, tokens["bob"], "calendar.remove", _remove(dropped))
            assert reader.status_code == 403
            assert reader.json()["error"]["code"] == "ACCESS_DENIED"
            assert reader.json()["error"]["fix"] == "remove it through Diablo"
            refused = post(client, tokens["alice"], "calendar.remove",
                           _remove(dropped, said_by="agent", came_by="agent_guess"))
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "brain.record.needs_owner_words"
            assert len(fake_google.live()) == 1

            removed = post(client, tokens["alice"], "calendar.remove", _remove(dropped))
            assert removed.status_code == 200, removed.text
            assert removed.json()["status"] == "removed"
            assert fake_google.live() == []
            assert fake_google.requests()[-1] == ("DELETE", "/evt1")
            repeated_remove = post(client, tokens["alice"], "calendar.remove", _remove(dropped))
            assert repeated_remove.status_code == 200, repeated_remove.text
            assert repeated_remove.json()["status"] == "not_found"
            assert fake_google.requests().count(("DELETE", "/evt1")) == 1
    finally:
        runtime.close()


def test_the_mcp_tool_list_gains_bc_calendar_remove_only_while_calendar_entries_are_on(tmp_path, monkeypatch, fake_google):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on, _, _ = _hub(tmp_path / "on", monkeypatch, review=True, google=True)
    off, _, _ = _hub(tmp_path / "off", monkeypatch, review=True, google=False)
    try:
        with TestClient(on.app), TestClient(off.app):
            on_names = {tool.name for tool in anyio.run(on.mcp_server.list_tools)}
            off_names = {tool.name for tool in anyio.run(off.mcp_server.list_tools)}
        assert on_names == off_names
        assert not {"bc_calendar_remove", "bc_work", "bc_project_save"} & on_names
        assert len(on_names) == 22
        assert len(off_names) == 22
    finally:
        on.close()
        off.close()


@pytest.mark.parametrize(("review", "google"), [(True, False), (False, True), (False, False)])
def test_off_nothing_changes_and_calendar_remove_does_not_exist(tmp_path, monkeypatch, fake_google, review, google):
    runtime, tokens, _ = _hub(tmp_path, monkeypatch, review=review, google=google)
    try:
        assert runtime.calendar_writer is None
        assert runtime.review_sources.calendar_adds_timed is False
        with running(runtime) as client:
            saved = post(client, tokens["alice"], "memory.save", SAVE).json()
            assert "calendar" not in saved
            assert "calendar_suggestion" not in saved
            missing = post(client, tokens["alice"], "calendar.remove", _remove(saved))
            assert missing.status_code == 422
            assert missing.json()["error"]["message"] == "unsupported Brain operation"
            assert missing.json()["error"]["fix"] == "use an operation in this listener's advertised tool list"
        assert fake_google.calls == []
    finally:
        runtime.close()


def test_a_credential_from_the_old_read_only_consent_stops_the_hub_at_startup(tmp_path, monkeypatch):
    """Exactly calendar.events plus gmail.readonly: the pre-N8 pair is refused loudly, so a stale
    consent is found at the restart, never by a silent failure later."""

    config, _ = _config(tmp_path, monkeypatch, review=True, google=True, scopes=(OLD_CALENDAR_READ_ONLY, GMAIL_READ_ONLY))
    with pytest.raises(ConfigurationError, match="invalid Google credential configuration"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)
