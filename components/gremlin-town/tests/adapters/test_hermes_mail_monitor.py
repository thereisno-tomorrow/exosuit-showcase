"""Diablo's important-mail monitor (ROA-91): Hermes's own scorer and rule, read-only Gmail, never silent."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "adapters" / "hermes" / "scripts" / "mail_monitor.py"
spec = importlib.util.spec_from_file_location("mail_monitor", SCRIPT)
assert spec is not None and spec.loader is not None
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

NOW = 1_790_000_000.0
LOGIN = {
    "type": "authorized_user",
    "client_id": "id.apps.googleusercontent.com",
    "client_secret": "client-secret",
    "refresh_token": "refresh-secret",
    "token": "old-access",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [mm.GMAIL_READ_ONLY_SCOPE],
}


class FakeGmail:
    """Google's token endpoint and the two Gmail reads the monitor makes."""

    def __init__(self, inbox: list[str] | None = None, scope: str = mm.GMAIL_READ_ONLY_SCOPE) -> None:
        self.inbox = list(inbox or [])  # newest first, like Gmail
        self.scope = scope
        self.calls: list[str] = []
        self.queries: list[str] = []

    def __call__(self, url: str, data: dict[str, str] | None, token: str | None) -> dict[str, Any]:
        self.calls.append(url.split("?")[0])
        if url == LOGIN["token_uri"]:
            assert data is not None and data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "refresh-secret"
            return {"access_token": "fresh-access", "scope": self.scope, "expires_in": 3599}
        assert token == "fresh-access"
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if url.startswith(f"{mm.API_BASE}/messages?"):
            self.queries.append(query["q"])
            return {"messages": [{"id": i, "threadId": i} for i in self.inbox]}
        message_id = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[1])
        return {
            "id": message_id,
            "snippet": f"snippet of {message_id}",
            "payload": {"headers": [
                {"name": "From", "value": f"Sender {message_id} <{message_id}@example.test>"},
                {"name": "Subject", "value": f"Subject {message_id}"},
                {"name": "Date", "value": "Tue, 29 Sep 2026 10:00:00 +0000"},
            ]},
        }


class FakeScorer:
    """Stands in for Hermes's scorer: a score per ID, and a record of each batch."""

    def __init__(self, scores: dict[str, int] | None = None, default: int = 2) -> None:
        self.scores = scores or {}
        self.default = default
        self.batches: list[list[str]] = []

    def __call__(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.batches.append([item["id"] for item in items])
        return [
            {"id": item["id"], "score": self.scores.get(item["id"], self.default), "reason": "needs a reply today",
             "item": item}
            for item in items
        ]


@pytest.fixture(autouse=True)
def isolated_token_env(monkeypatch):
    monkeypatch.delenv("MAIL_MONITOR_TOKEN_PATH", raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "google_token_mail.json").write_text(json.dumps(LOGIN), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("entrypoint", [mm.run, mm.check])
def test_agent_login_is_never_a_fallback(tmp_path, entrypoint):
    (tmp_path / "google_token.json").write_text(json.dumps(LOGIN), encoding="utf-8")
    gmail = FakeGmail()
    with pytest.raises(mm.MonitorError, match="google_token_mail.json"):
        entrypoint(tmp_path, http=gmail)
    assert gmail.calls == []


@pytest.mark.parametrize("entrypoint", [mm.run, mm.check])
def test_override_has_no_fallback_even_when_default_monitor_login_exists(home, monkeypatch, entrypoint):
    monkeypatch.setenv("MAIL_MONITOR_TOKEN_PATH", str(home / "missing-token.json"))
    gmail = FakeGmail()
    with pytest.raises(mm.MonitorError, match="missing-token.json"):
        entrypoint(home, http=gmail)
    assert gmail.calls == []


@pytest.mark.parametrize("entrypoint", [mm.run, mm.check])
def test_override_file_is_used_and_a_wide_grant_is_refused(home, monkeypatch, entrypoint):
    alternate = home / "separate.json"
    alternate.write_text(json.dumps({**LOGIN, "refresh_token": "alternate-refresh"}), encoding="utf-8")
    monkeypatch.setenv("MAIL_MONITOR_TOKEN_PATH", str(alternate))
    calls = []

    def wide_login(url, data, token):
        assert data["refresh_token"] == "alternate-refresh"
        calls.append(url)
        return {"access_token": "fake-access", "scope": "https://www.googleapis.com/auth/gmail.modify"}

    with pytest.raises(mm.MonitorError, match="must be read-only"):
        entrypoint(home, http=wide_login)
    assert calls == [LOGIN["token_uri"]]


def test_monitor_uses_its_login_even_when_agent_has_a_wide_login(home):
    agent = home / "google_token.json"
    agent.write_text(json.dumps({**LOGIN, "refresh_token": "agent-refresh",
                                 "scopes": ["https://www.googleapis.com/auth/gmail.modify"]}), encoding="utf-8")
    before = agent.read_bytes()
    assert mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW) == mm.FIRST_RUN
    assert agent.read_bytes() == before


def started(home: Path, cursor: float = NOW - 900, seen: dict[str, float] | None = None) -> None:
    mm.save_state(home / "mail-monitor" / "state.json", {"cursor": cursor, "seen": seen or {}})


def state(home: Path) -> dict[str, Any]:
    return json.loads((home / "mail-monitor" / "state.json").read_text(encoding="utf-8"))


def test_the_rule_is_hermess_catalog_default_and_the_bar_is_seven():
    assert mm.RULE == (
        "surface only mail that needs a reply today, is from a manager/family member, or mentions a deadline"
    )
    assert mm.THRESHOLD == 7


def test_the_first_run_says_it_is_on_and_starts_from_now(home):
    gmail = FakeGmail(inbox=["old1", "old2"])

    output = mm.run(home, http=gmail, scorer=FakeScorer(), now=lambda: NOW)

    assert output == mm.FIRST_RUN
    assert state(home) == {"cursor": NOW, "seen": {}}
    assert gmail.calls == [LOGIN["token_uri"]]  # no inbox read, no backlog of pings


def test_an_important_email_is_pinged_and_the_rest_stay_quiet(home):
    started(home)
    scorer = FakeScorer({"m2": 8})

    output = mm.run(home, http=FakeGmail(inbox=["m2", "m1"]), scorer=scorer, now=lambda: NOW)

    assert output.startswith("Important email (8/10)\nFrom: Sender m2 <m2@example.test>\nSubject: Subject m2")
    assert "https://mail.google.com/mail/u/0/#all/m2" in output
    assert "Subject m1" not in output
    assert output.endswith(mm.FOOTER)
    assert scorer.batches == [["m2", "m1"]]
    assert state(home) == {"cursor": NOW, "seen": {"m1": NOW, "m2": NOW}}


def test_nothing_important_sends_nothing(home):
    started(home)

    assert mm.run(home, http=FakeGmail(inbox=["m1"]), scorer=FakeScorer(default=6), now=lambda: NOW) == ""
    assert state(home)["seen"] == {"m1": NOW}


def flagged(home):
    return json.loads((home / "mail-monitor" / mm.FLAGGED_LOG_NAME).read_text(encoding="utf-8"))["emails"]


def test_flagged_log_uses_existing_scores_and_never_logs_body_or_login(home):
    started(home)
    scorer = FakeScorer({"m2": 7, "m1": 6})
    mm.run(home, http=FakeGmail(inbox=["m2", "m1"]), scorer=scorer, now=lambda: NOW)
    assert scorer.batches == [["m2", "m1"]]
    row, = flagged(home)
    assert row == dict(message_id="m2", sender="Sender m2 <m2@example.test>", subject="Subject m2",
                       score=7, reason="needs a reply today", link=mm.GMAIL_LINK.format(message_id="m2"), flagged_at=NOW)


def test_flagged_log_prunes_on_quiet_runs_and_deduplicates_retries(home):
    path = home / "mail-monitor" / mm.FLAGGED_LOG_NAME
    item = {"id": "m1", "from": "sender", "subject": "subject", "url": "link"}
    mm.log_flagged(path, [item], {"m1": (8, "reason")}, NOW)
    mm.log_flagged(path, [item], {"m1": (8, "reason")}, NOW + 60)
    assert len(flagged(home)) == 1
    assert flagged(home)[0]["flagged_at"] == NOW
    started(home)
    mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW + mm.FLAGGED_SECONDS + 1)
    assert flagged(home) == []


def test_bad_flagged_log_fails_loudly_before_cursor_moves(home):
    started(home)
    before = state(home)
    (home / "mail-monitor" / mm.FLAGGED_LOG_NAME).write_text("broken", encoding="utf-8")
    with pytest.raises(mm.MonitorError, match="flagged-mail log"):
        mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW)
    assert state(home) == before


def test_only_the_log_requests_group_read_mode_on_each_atomic_replacement(home, monkeypatch):
    opened = []
    original = mm.os.open
    def record_open(path, flags, mode=0o777, **kwargs):
        opened.append((Path(path).name, mode))
        return original(path, flags, mode, **kwargs)
    monkeypatch.setattr(mm.os, "open", record_open)
    mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW)
    mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW + 60)
    writes = [entry for entry in opened if entry[0].endswith(".tmp")]
    assert writes == [(mm.FLAGGED_LOG_NAME + ".tmp", 0o640), ("state.json.tmp", 0o600)] * 2


def test_mail_is_looked_up_from_an_hour_before_the_last_good_run(home):
    started(home, cursor=NOW - 900)
    gmail = FakeGmail()

    mm.run(home, http=gmail, scorer=FakeScorer(), now=lambda: NOW)

    assert gmail.queries == [f"in:inbox after:{int(NOW - 900) - 3600}"]


def test_an_email_already_seen_is_not_scored_or_pinged_again(home):
    started(home, seen={"m1": NOW - 600})
    scorer = FakeScorer(default=9)

    assert mm.run(home, http=FakeGmail(inbox=["m1"]), scorer=scorer, now=lambda: NOW) == ""
    assert scorer.batches == []


def test_an_email_the_scorer_gave_no_score_is_an_error_and_nothing_is_marked_seen(home):
    started(home)

    def drops_one(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": items[0]["id"], "score": 9, "reason": "x"}]

    with pytest.raises(mm.MonitorError, match="no readable score for 1 of 2"):
        mm.run(home, http=FakeGmail(inbox=["m2", "m1"]), scorer=drops_one, now=lambda: NOW)
    assert state(home)["seen"] == {}


@pytest.mark.parametrize("bad", ["9", 8.5, None, True])
def test_a_score_that_isnt_a_whole_number_counts_as_no_score(home, bad):
    started(home)

    def odd(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": item["id"], "score": bad} for item in items]

    with pytest.raises(mm.MonitorError, match="no readable score"):
        mm.run(home, http=FakeGmail(inbox=["m1"]), scorer=odd, now=lambda: NOW)


def test_emails_go_to_the_scorer_ten_at_a_time(home):
    started(home)
    scorer = FakeScorer()

    mm.run(home, http=FakeGmail(inbox=[f"m{i}" for i in range(23)]), scorer=scorer, now=lambda: NOW)

    assert [len(batch) for batch in scorer.batches] == [10, 10, 3]


def test_a_flood_is_scored_thirty_a_run_and_the_window_waits_for_the_rest(home):
    started(home, cursor=NOW - 900)
    inbox = [f"m{i}" for i in range(35)]
    scorer = FakeScorer()

    mm.run(home, http=FakeGmail(inbox=inbox), scorer=scorer, now=lambda: NOW)
    assert sum(len(b) for b in scorer.batches) == 30
    assert state(home)["cursor"] == NOW - 900

    scorer.batches.clear()
    mm.run(home, http=FakeGmail(inbox=inbox), scorer=scorer, now=lambda: NOW + 900)
    assert scorer.batches == [[f"m{i}" for i in range(30, 35)]]
    assert state(home)["cursor"] == NOW + 900


def test_seen_ids_are_forgotten_after_three_days(home):
    started(home, seen={"old": NOW - 4 * 24 * 3600, "recent": NOW - 3600})

    mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW)

    assert state(home)["seen"] == {"recent": NOW - 3600}


def test_a_login_that_can_do_more_than_read_mail_is_refused_before_any_mail_is_read(home):
    started(home)
    gmail = FakeGmail(inbox=["m1"], scope=f"{mm.GMAIL_READ_ONLY_SCOPE} https://www.googleapis.com/auth/gmail.send")

    with pytest.raises(mm.MonitorError, match="must be read-only"):
        mm.run(home, http=gmail, scorer=FakeScorer(), now=lambda: NOW)
    assert gmail.calls == [LOGIN["token_uri"]]


def test_no_login_says_how_to_make_one(tmp_path):
    with pytest.raises(mm.MonitorError, match="google_consent.py --for diablo-mail"):
        mm.run(tmp_path, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW)


def test_a_login_file_without_a_token_uri_uses_googles(tmp_path):
    body = {k: v for k, v in LOGIN.items() if k != "token_uri"}
    (tmp_path / "google_token_mail.json").write_text(json.dumps(body), encoding="utf-8")

    assert mm.load_login(tmp_path / "google_token_mail.json")["token_uri"] == mm.DEFAULT_TOKEN_URI


def test_a_login_file_missing_its_refresh_token_is_refused(tmp_path):
    body = {k: v for k, v in LOGIN.items() if k != "refresh_token"}
    (tmp_path / "google_token_mail.json").write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(mm.MonitorError, match="no refresh_token"):
        mm.load_login(tmp_path / "google_token_mail.json")


def test_the_login_file_is_read_and_never_written(home):
    before = (home / "google_token_mail.json").read_bytes()
    started(home)

    mm.run(home, http=FakeGmail(inbox=["m1"]), scorer=FakeScorer(default=9), now=lambda: NOW)

    assert (home / "google_token_mail.json").read_bytes() == before


def test_a_ping_fits_in_what_a_telegram_reply_quotes_back():
    item = {"id": "m1", "from": "x" * 400, "subject": "s " * 400, "url": mm.GMAIL_LINK.format(message_id="m1")}

    text = mm.ping(item, 9, "r " * 400)

    assert len(text) <= mm.PING_CHARS < 500
    assert text.startswith("Important email (9/10)\nFrom: ")


def test_main_reports_a_failure_on_stderr_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert mm.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Mail monitor: No Gmail login")


def test_main_prints_the_pings_and_nothing_secret(home, monkeypatch, capsys):
    started(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(mm, "run", _run_with(FakeGmail(inbox=["m1"]), FakeScorer(default=9)))

    assert mm.main() == 0
    out = capsys.readouterr().out
    assert "Important email (9/10)" in out
    for secret in ("refresh-secret", "client-secret", "fresh-access"):
        assert secret not in out


def _run_with(gmail: FakeGmail, scorer: FakeScorer):
    original = mm.__dict__["run"]

    def run(home: Path) -> str:
        return original(home, http=gmail, scorer=scorer, now=lambda: NOW)

    return run


# ---------------------------------------------------------------------------- Hermes's scorer, as a subprocess

FAKE_CLASSIFIER = textwrap.dedent('''
    """Stand-in for Hermes v0.20.5 cron/scripts/classify_items.py: the same flags and JSON shape."""
    import argparse, json, os, sys
    p = argparse.ArgumentParser()
    p.add_argument("--criteria", required=True)
    p.add_argument("--threshold", type=int, default=7)
    p.add_argument("--format", default="text")
    a = p.parse_args()
    items = json.loads(sys.stdin.read() or "[]")
    record = os.environ.get("FAKE_CLASSIFIER_RECORD")
    if record:
        open(record, "w").write(json.dumps({"argv": sys.argv[1:], "items": items}))
    mode = os.environ.get("FAKE_CLASSIFIER_MODE", "ok")
    if mode == "fail":
        print("classify_items: classifier call failed: 401", file=sys.stderr)
        sys.exit(4)
    if mode == "garbled":
        print("classify_items: could not parse classifier output", file=sys.stderr)
        sys.exit(0)
    if '"index": <int>' not in a.criteria:
        # What GPT-6 Luna did live (29 Sep): told only the rule, it answered [{"score": 5}] with no
        # "index", which Hermes's parser drops without a word; nothing printed, exit 0.
        sys.exit(0)
    out = [{"id": it["id"], "score": 8, "reason": "deadline", "item": it} for it in items
           if 8 >= a.threshold]
    if out:
        print(json.dumps(out, indent=2))
''')


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    package = tmp_path / "hermes" / "cron" / "scripts"
    package.mkdir(parents=True)
    (tmp_path / "hermes" / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "classify_items.py").write_text(FAKE_CLASSIFIER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "hermes"))
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLASSIFIER_RECORD", str(record))
    return record


ITEMS = [{"id": "m1", "from": "a", "subject": "s", "summary": "x", "url": "u"}]


def test_hermess_scorer_is_asked_with_the_rule_threshold_zero_and_json(fake_hermes):
    answer = mm.hermes_scorer(ITEMS)

    assert [(entry["id"], entry["score"]) for entry in answer] == [("m1", 8)]
    sent = json.loads(fake_hermes.read_text(encoding="utf-8"))
    assert sent["items"] == ITEMS
    argv = sent["argv"]
    assert argv[argv.index("--criteria") + 1] == mm.CRITERIA
    assert argv[argv.index("--threshold") + 1] == "0"
    assert argv[argv.index("--format") + 1] == "json"


def test_hermess_scorer_failing_is_an_error_with_its_words(fake_hermes, monkeypatch):
    monkeypatch.setenv("FAKE_CLASSIFIER_MODE", "fail")

    with pytest.raises(mm.MonitorError, match="exit 4.*classifier call failed"):
        mm.hermes_scorer(ITEMS)


def test_hermess_scorer_going_quiet_on_garbled_output_is_caught_with_its_words(fake_hermes, monkeypatch):
    monkeypatch.setenv("FAKE_CLASSIFIER_MODE", "garbled")

    with pytest.raises(mm.MonitorError, match="no readable score for any of 1.*could not parse") as caught:
        mm.score_all(ITEMS, mm.hermes_scorer)
    assert caught.value.cause == "scorer gave no scores"


def test_the_criteria_carry_the_rule_word_for_word_and_the_answer_format():
    assert mm.CRITERIA.startswith(mm.RULE + "\n\n")
    assert mm.CRITERIA.endswith(mm.FORMAT)
    assert '[{"index": <int>, "score": <int 0-10>, "reason": "<short>"}]' in mm.FORMAT
    assert "Return ONLY a JSON array, one object per item, in the same order" in mm.FORMAT


def test_without_the_format_the_scorer_says_nothing_and_that_is_an_error(fake_hermes, monkeypatch):
    monkeypatch.setattr(mm, "CRITERIA", mm.RULE)  # what Hermes itself sends: the rule alone

    with pytest.raises(mm.MonitorError, match="no readable score for any of 1.*Scorer said: nothing"):
        mm.hermes_scorer(ITEMS)


def test_the_script_runs_on_the_standard_library_alone():
    source = SCRIPT.read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    }
    assert imported <= set(sys.stdlib_module_names)


# ---------------------------------------------------------------------------- the install check


def test_the_check_reads_the_inbox_and_finds_the_scorer_without_saving_anything(home, fake_hermes, monkeypatch):
    monkeypatch.syspath_prepend(str(fake_hermes.parent / "hermes"))
    gmail = FakeGmail(inbox=["m1", "m2"])

    text = mm.check(home, http=gmail)

    assert text.startswith("Mail monitor check: the Gmail login works and is read-only; 2 inbox emails")
    assert not (home / "mail-monitor").exists()


def test_the_check_fails_without_hermess_scorer(home, monkeypatch):
    for name in [n for n in sys.modules if n == "cron" or n.startswith("cron.")]:
        monkeypatch.delitem(sys.modules, name)  # the stand-in another test imported

    with pytest.raises(mm.MonitorError, match="isn't importable"):
        mm.check(home, http=FakeGmail())


def test_the_check_refuses_a_login_that_can_send(home):
    gmail = FakeGmail(scope=f"{mm.GMAIL_READ_ONLY_SCOPE} https://www.googleapis.com/auth/gmail.send")

    with pytest.raises(mm.MonitorError, match="must be read-only"):
        mm.check(home, http=gmail)


def test_main_runs_the_check_only_when_asked(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(mm, "check", lambda h: "checked")
    monkeypatch.setattr(mm, "run", lambda h: pytest.fail("the check must not run the monitor"))

    assert mm.main(["--check"]) == 0
    assert capsys.readouterr().out == "checked\n"


# ---------------------------------------------------------------------------- alert once, then quiet until it recovers


def failure(home: Path) -> dict[str, Any]:
    return json.loads(mm.failure_path(home).read_text(encoding="utf-8"))


def test_the_first_failure_alerts_and_is_remembered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert mm.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Mail monitor: No Gmail login")
    assert mm.QUIET_AFTER in err
    assert failure(tmp_path)["repeats"] == 0


def test_the_same_failure_again_is_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mm.main([])
    capsys.readouterr()

    assert mm.main([]) == 0
    assert mm.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert failure(tmp_path)["repeats"] == 2


def test_the_same_kind_of_failure_with_other_numbers_is_still_a_repeat(tmp_path, capsys):
    assert mm.report_failure(tmp_path, mm.MonitorError("no readable score for 1 of 2 emails")) == 1
    assert mm.report_failure(tmp_path, mm.MonitorError("no readable score for 3 of 5 emails")) == 0


def test_a_different_failure_alerts_again(tmp_path, capsys):
    assert mm.report_failure(tmp_path, mm.MonitorError("Google answered 401", cause="google 401 token")) == 1
    assert mm.report_failure(tmp_path, mm.MonitorError("Hermes's scorer failed", cause="scorer exit 4")) == 1
    err = capsys.readouterr().err
    assert "Google answered 401" in err and "Hermes's scorer failed" in err
    assert failure(tmp_path)["cause"] == "scorer exit 4"


def test_a_good_run_after_a_failure_says_it_is_working_again_once(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(home))
    mm.report_failure(home, mm.MonitorError("Couldn't reach Gmail", cause="unreachable gmail"))
    capsys.readouterr()
    monkeypatch.setattr(mm, "run", lambda h: "")

    assert mm.main([]) == 0
    assert capsys.readouterr().out == mm.RECOVERED + "\n"
    assert not mm.failure_path(home).exists()

    assert mm.main([]) == 0
    assert capsys.readouterr().out == ""


def test_the_recovery_line_comes_before_the_pings(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(home))
    mm.report_failure(home, mm.MonitorError("x", cause="x"))
    capsys.readouterr()
    monkeypatch.setattr(mm, "run", lambda h: "Important email (8/10)")

    mm.main([])

    assert capsys.readouterr().out == mm.RECOVERED + "\n\nImportant email (8/10)\n"


def test_a_failed_run_never_moves_the_cursor_or_marks_mail_seen(home, monkeypatch, capsys):
    started(home, cursor=NOW - 900, seen={"old": NOW - 600})
    before = (home / "mail-monitor" / "state.json").read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(home))

    def broken(items):
        raise mm.MonitorError("Hermes's scorer failed (exit 4): 401", cause="scorer exit 4")

    monkeypatch.setattr(mm, "run", _run_with(FakeGmail(inbox=["m1"]), broken))

    assert mm.main([]) == 1
    assert mm.main([]) == 0
    assert (home / "mail-monitor" / "state.json").read_bytes() == before


def test_an_unexpected_crash_alerts_once_with_its_trace(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def crash(h):
        raise KeyError("subject")

    monkeypatch.setattr(mm, "run", crash)

    assert mm.main([]) == 1
    err = capsys.readouterr().err
    assert "unexpected KeyError" in err and "Traceback" in err
    assert mm.main([]) == 0


def test_an_unreadable_failure_file_errs_towards_alerting(tmp_path, capsys):
    mm.failure_path(tmp_path).parent.mkdir(parents=True)
    mm.failure_path(tmp_path).write_text("not json", encoding="utf-8")

    assert mm.report_failure(tmp_path, mm.MonitorError("x", cause="x")) == 1


def test_known_failures_carry_a_stable_cause(fake_hermes, monkeypatch):
    monkeypatch.setenv("FAKE_CLASSIFIER_MODE", "fail")

    with pytest.raises(mm.MonitorError) as caught:
        mm.hermes_scorer(ITEMS)
    assert caught.value.cause == "scorer exit 4"


def test_the_check_is_never_silenced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mm.report_failure(tmp_path, mm.MonitorError("No Gmail login at x", cause="No Gmail login at x"))
    capsys.readouterr()

    assert mm.main(["--check"]) == 1
    assert "No Gmail login" in capsys.readouterr().err
