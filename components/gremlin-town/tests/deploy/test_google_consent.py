"""Nic's one-time Google consent helper (step D, ROA-18): the file it writes is one the hub loads."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from gremlin_town.sources.google.auth import ALLOWED_SCOPES, GoogleCredential

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "google_consent.py"
spec = importlib.util.spec_from_file_location("google_consent", SCRIPT)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

BOTH = " ".join(script.SCOPES)
CLIENT = {"client_id": "id.apps.googleusercontent.com", "client_secret": "client-secret"}


def client_file(tmp_path: Path, body: Any) -> Path:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_the_scopes_are_the_pair_the_hub_enforces():
    assert frozenset(script.SCOPES) == ALLOWED_SCOPES


def test_a_desktop_client_file_gives_its_id_and_secret(tmp_path):
    path = client_file(tmp_path, {"installed": {**CLIENT, "redirect_uris": ["http://localhost"]}})

    assert script.load_client(path) == CLIENT


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ({"web": CLIENT}, "Desktop app"),
        ({"other": {}}, '"installed"'),
        ({"installed": {"client_id": "id"}}, "client_secret"),
        ([], "isn't the one"),
    ],
)
def test_a_wrong_client_file_is_refused_with_what_to_do(tmp_path, body, words):
    with pytest.raises(script.ConsentError, match=words):
        script.load_client(client_file(tmp_path, body))


def test_the_consent_link_asks_for_exactly_the_pair_and_a_refresh_token():
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        script.consent_url("id", "http://127.0.0.1:5000/", "state", "challenge")
    ).query))

    assert set(query["scope"].split()) == ALLOWED_SCOPES
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256"


@pytest.mark.parametrize(
    ("granted", "words"),
    [
        (script.CALENDAR_EVENTS_SCOPE, "Read your email"),
        (script.GMAIL_READ_ONLY_SCOPE, "See and change events"),
        (BOTH + " https://www.googleapis.com/auth/drive", "more than asked"),
    ],
)
def test_anything_but_exactly_the_pair_is_refused(granted, words):
    assert words in script.scope_problem(granted)


def test_the_pair_in_any_order_passes():
    assert script.scope_problem(" ".join(reversed(script.SCOPES))) is None


def test_no_refresh_token_is_refused():
    with pytest.raises(script.ConsentError, match="no refresh token"):
        script.credential_record(CLIENT, {"scope": BOTH}, "nic@example.test")


def test_an_existing_credential_is_never_replaced_without_force(tmp_path):
    out = tmp_path / "google-credential.json"
    out.write_text("keep", encoding="utf-8")

    with pytest.raises(script.ConsentError, match="--force"):
        script.write_credential(out, {}, force=False)
    assert out.read_text(encoding="utf-8") == "keep"


def test_the_whole_flow_writes_a_credential_the_hub_loads(tmp_path, monkeypatch, capsys):
    calls: list[tuple[str, dict[str, str] | None, str | None]] = []

    def fake_google(url, *, data=None, token=None):
        calls.append((url, data, token))
        if url == script.TOKEN_URL:
            return {"access_token": "access", "refresh_token": "refresh", "scope": BOTH}
        if url == script.PROFILE_URL:
            return {"emailAddress": "nic@example.test"}
        return {"items": []}

    def consent_given(url: str) -> bool:
        # Stands in for Nic approving in the browser: Google redirects to the loopback with a code.
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        answer = query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "one-time", "state": query["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(answer, timeout=5).read(), daemon=True).start()
        return True

    monkeypatch.setattr(script, "_request", fake_google)
    monkeypatch.setattr(script.webbrowser, "open", consent_given)
    out = tmp_path / "secrets" / "google-credential.json"

    assert script.main([str(client_file(tmp_path, {"installed": CLIENT})), "--out", str(out)]) == 0

    loaded = GoogleCredential.load(out)
    assert (loaded.account, loaded.refresh_token, loaded.scopes) == ("nic@example.test", "refresh", ALLOWED_SCOPES)
    exchange = calls[0][1]
    assert exchange is not None and exchange["code"] == "one-time" and exchange["code_verifier"]
    assert [url for url, _data, _token in calls[1:]] == [script.PROFILE_URL, script.CALENDAR_CHECK_URL]
    printed = capsys.readouterr()
    assert "refresh" not in printed.out + printed.err
    assert "client-secret" not in printed.out + printed.err


def test_a_refused_consent_stops_without_writing(tmp_path, monkeypatch, capsys):
    def refused(url: str) -> bool:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        answer = query["redirect_uri"] + "?" + urllib.parse.urlencode({"error": "access_denied", "state": query["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(answer, timeout=5).read(), daemon=True).start()
        return True

    monkeypatch.setattr(script.webbrowser, "open", refused)
    out = tmp_path / "google-credential.json"

    assert script.main([str(client_file(tmp_path, {"installed": CLIENT})), "--out", str(out)]) == 1
    assert "access_denied" in capsys.readouterr().err
    assert not out.exists()


# ---------------------------------------------------------------------------- Diablo's read-only mail (ROA-91)

MONITOR = ROOT / "adapters" / "hermes" / "scripts" / "mail_monitor.py"
monitor_spec = importlib.util.spec_from_file_location("mail_monitor_for_consent", MONITOR)
assert monitor_spec is not None and monitor_spec.loader is not None
monitor = importlib.util.module_from_spec(monitor_spec)
monitor_spec.loader.exec_module(monitor)

READ_ONLY = script.GMAIL_READ_ONLY_SCOPE


def test_diablo_asks_for_read_only_mail_and_nothing_else():
    assert script.DIABLO.scopes == (READ_ONLY,) == (monitor.GMAIL_READ_ONLY_SCOPE,)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        script.consent_url("id", "http://127.0.0.1:5000/", "state", "challenge", script.DIABLO.scopes)
    ).query))
    assert query["scope"] == READ_ONLY


@pytest.mark.parametrize(
    ("granted", "words"),
    [
        ("", "Read your email"),
        (BOTH, "more than asked"),
        (READ_ONLY + " https://www.googleapis.com/auth/gmail.send", "more than asked"),
    ],
)
def test_diablo_refuses_anything_but_read_only_mail(granted, words):
    assert words in script.scope_problem(granted, script.DIABLO.scopes)


def test_diablos_file_is_what_hermes_and_the_monitor_read(tmp_path):
    from datetime import UTC, datetime

    record = script.hermes_token_record(
        CLIENT, {"access_token": "access", "refresh_token": "refresh", "scope": READ_ONLY, "expires_in": 3599},
        "nic@example.test", now=datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
    )

    assert record == {
        "type": "authorized_user",
        "token": "access",
        "refresh_token": "refresh",
        "token_uri": script.TOKEN_URL,
        "client_id": CLIENT["client_id"],
        "client_secret": CLIENT["client_secret"],
        "scopes": [READ_ONLY],
        "account": "nic@example.test",
        "expiry": "2026-09-29T15:58:59Z",  # google-auth parses this shape; Hermes refreshes once it passes
    }
    path = tmp_path / "google_token.json"
    script.write_credential(path, record, force=False)
    assert monitor.load_login(path) == {
        "client_id": CLIENT["client_id"], "client_secret": CLIENT["client_secret"],
        "refresh_token": "refresh", "token_uri": script.TOKEN_URL,
    }


def test_diablos_file_is_refused_with_a_calendar_scope_too():
    with pytest.raises(script.ConsentError, match="more than asked"):
        script.hermes_token_record(CLIENT, {"access_token": "a", "refresh_token": "r", "scope": BOTH}, "n")


def test_the_whole_diablo_flow_reads_no_calendar_and_prints_nothing_secret(tmp_path, monkeypatch, capsys):
    calls: list[str] = []

    def fake_google(url, *, data=None, token=None):
        calls.append(url)
        if url == script.TOKEN_URL:
            return {"access_token": "access-secret", "refresh_token": "refresh", "scope": READ_ONLY, "expires_in": 3599}
        return {"emailAddress": "nic@example.test"}

    def consent_given(url: str) -> bool:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert query["scope"] == READ_ONLY
        answer = query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "one-time", "state": query["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(answer, timeout=5).read(), daemon=True).start()
        return True

    monkeypatch.setattr(script, "_request", fake_google)
    monkeypatch.setattr(script.webbrowser, "open", consent_given)
    out = tmp_path / "diablo-google-token.json"

    assert script.main([str(client_file(tmp_path, {"installed": CLIENT})), "--for", "diablo", "--out", str(out)]) == 0

    assert calls == [script.TOKEN_URL, script.PROFILE_URL]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["scopes"] == [READ_ONLY] and written["refresh_token"] == "refresh"
    printed = capsys.readouterr()
    assert "read-only" in printed.out and "mail-monitor-01-install" in printed.out
    for secret in ("refresh", "client-secret", "access-secret"):
        assert secret not in printed.out + printed.err


def test_the_default_targets_and_outputs():
    assert script.TARGETS == {"hub": script.HUB, "diablo": script.DIABLO, "diablo-mail": script.DIABLO_MAIL}
    assert script.HUB.scopes == script.SCOPES and script.HUB.default_out == script.DEFAULT_OUT
    assert script.DIABLO.default_out.name == "diablo-google-token.json"
    assert script.DIABLO.default_out != script.HUB.default_out
    assert script.DIABLO_MAIL.scopes == script.DIABLO.scopes
    assert script.DIABLO_MAIL.default_out.name == "diablo-google-token-mail.json"


@pytest.mark.parametrize("granted", [
    "", READ_ONLY, BOTH,
    script.GMAIL_MODIFY_SCOPE + " " + READ_ONLY,
    script.GMAIL_MODIFY_SCOPE + " https://www.googleapis.com/auth/gmail.send",
    script.GMAIL_MODIFY_SCOPE + " https://www.googleapis.com/auth/drive",
])
def test_edit_login_refuses_every_other_scope_set(granted):
    with pytest.raises(script.ConsentError):
        script.hermes_token_record(
            CLIENT, {"access_token": "a", "refresh_token": "r", "scope": granted},
            "user@example.test", scopes=script.DIABLO_EDIT.scopes,
        )


@pytest.mark.parametrize("target", ["hub", "diablo-mail"])
def test_edit_flag_is_only_for_diablo(tmp_path, target):
    with pytest.raises(SystemExit) as caught:
        script.main(["unused.json", "--for", target, "--gmail-edit"])
    assert caught.value.code == 2


def test_edit_flow_requests_and_writes_modify_alone(tmp_path, monkeypatch, capsys):
    class Server:
        server_port = 5000

        def server_close(self):
            pass

    calls = []
    opened = []
    monkeypatch.setattr(script.http.server, "HTTPServer", lambda *a: Server())
    monkeypatch.setattr(script, "_wait_for_code", lambda *a: "code")
    monkeypatch.setattr(script.webbrowser, "open", lambda url: opened.append(url))

    def fake_google(url, **kwargs):
        calls.append(url)
        if url == script.TOKEN_URL:
            return {"access_token": "fake-access", "refresh_token": "fake-refresh",
                    "scope": script.GMAIL_MODIFY_SCOPE, "expires_in": 3600}
        return {"emailAddress": "user@example.test"}

    monkeypatch.setattr(script, "_request", fake_google)
    out = tmp_path / "edit.json"
    assert script.main([str(client_file(tmp_path, {"installed": CLIENT})),
                        "--for", "diablo", "--gmail-edit", "--out", str(out)]) == 0
    assert calls == [script.TOKEN_URL, script.PROFILE_URL]
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["scope"] == script.GMAIL_MODIFY_SCOPE
    assert json.loads(out.read_text())["scopes"] == [script.GMAIL_MODIFY_SCOPE]
    assert "login can send" in capsys.readouterr().out


def test_hermes_record_cannot_be_asked_for_unrestricted_scopes():
    with pytest.raises(script.ConsentError, match="only gmail.readonly"):
        script.hermes_token_record(CLIENT, {"refresh_token": "r"}, "user@example.test", scopes=script.SCOPES)


def test_mail_target_writes_a_separate_read_only_login(tmp_path, monkeypatch):
    # Reuse the simulated consent flow, but exercise the new target and its default output.
    target = script.DIABLO_MAIL._replace(default_out=tmp_path / script.DIABLO_MAIL.default_out.name)
    monkeypatch.setitem(script.TARGETS, "diablo-mail", target)

    class Server:
        server_port = 5000

        def server_close(self):
            pass

    calls = []
    monkeypatch.setattr(script.http.server, "HTTPServer", lambda *a: Server())
    monkeypatch.setattr(script, "_wait_for_code", lambda *a: "code")

    def fake_google(url, **kwargs):
        calls.append(url)
        if url == script.TOKEN_URL:
            return {"access_token": "fake-access", "refresh_token": "fake-refresh", "scope": READ_ONLY}
        return {"emailAddress": "user@example.test"}

    monkeypatch.setattr(script, "_request", fake_google)
    assert script.main([str(client_file(tmp_path, {"installed": CLIENT})),
                        "--for", "diablo-mail", "--no-browser"]) == 0
    record = json.loads(target.default_out.read_text())
    assert record["scopes"] == [READ_ONLY]
    assert monitor.load_login(target.default_out)["refresh_token"] == "fake-refresh"
    assert calls == [script.TOKEN_URL, script.PROFILE_URL]
