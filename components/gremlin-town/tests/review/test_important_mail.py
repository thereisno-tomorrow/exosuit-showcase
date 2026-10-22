"""Local file plus mocked unread labels: no mail, model, record write or network."""

import json
import time
from threading import Event
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from brain_core.review import HistoryRow, SECTION_CAP, build_morning
from brain_core.timewords import today_in_singapore
from gremlin_town.review.collect import ReviewSources, SourceUnavailable, collect_morning_inputs
from gremlin_town.review.important_mail import EMAIL_REF_PREFIX, ImportantMailSource
from gremlin_town.review.run import ReviewContext, current_review_part, run_review
from gremlin_town.review.settings import ReviewSettings
from gremlin_town.review.why_log import WhyLog

NOW = datetime(2026, 10, 1, 22, tzinfo=UTC)


def email(message_id="fake1", **changes):
    return dict(message_id=message_id, sender="Sender <sender@example.test>", subject="Fixture subject",
                score=7, reason="reply today", link=f"https://mail.google.com/mail/u/0/#all/{message_id}",
                flagged_at=(NOW - timedelta(hours=1)).timestamp(), **changes)


def write_log(tmp_path, entries):
    path = tmp_path / "flagged-mail.json"
    path.write_text(json.dumps(dict(version=1, emails=entries)), encoding="utf-8")
    return path


def sources(source=None):
    return ReviewSources(brain_items=lambda: (), calendar_events=lambda *args: (), open_requests=lambda: (),
                         important_emails=source)


def history(day, message_id="fake1"):
    return HistoryRow(EMAIL_REF_PREFIX + message_id, "review", "important_email", "", None, day)


def test_only_new_high_scored_retained_unread_mail_is_checked(tmp_path):
    rows = [email("fake1"), email("fake2"), {**email("low"), "score": 6},
            {**email("old"), "flagged_at": (NOW - timedelta(days=4)).timestamp()},
            {**email("prior"), "flagged_at": (NOW - timedelta(days=1, hours=2)).timestamp()},
            {**email("future"), "flagged_at": (NOW + timedelta(hours=1)).timestamp()}]
    calls = []
    def unread(message_id):
        calls.append(message_id)
        return message_id == "fake1"
    source = ImportantMailSource(write_log(tmp_path, rows), unread)
    assert [row.message_id for row in source(NOW, NOW - timedelta(days=1), ())] == ["fake1"]
    assert calls == ["fake1", "fake2"]


def test_repeat_uses_distinct_delivered_mornings_and_keeps_current_day(tmp_path):
    source = ImportantMailSource(write_log(tmp_path, [email()]), lambda _: True)
    assert len(source(NOW + timedelta(days=1), NOW, (history(NOW), history(NOW)))) == 1
    two = (history(NOW), history(NOW + timedelta(days=1)))
    assert len(source(NOW + timedelta(days=1, hours=1), NOW, two)) == 1
    assert source(NOW + timedelta(days=2), NOW + timedelta(days=1), two) == ()


@pytest.mark.parametrize("bad", ["not json", '{}', '{"version":1,"emails":[{}]}'])
def test_bad_log_becomes_couldnt_see_and_does_not_call_google(tmp_path, bad):
    path = tmp_path / "log.json"
    path.write_text(bad, encoding="utf-8")
    def forbidden(_):
        pytest.fail("invalid log must not read labels")
    inputs = collect_morning_inputs(sources(ImportantMailSource(path, forbidden)), (), NOW)
    look = build_morning(inputs).parts[0]
    assert any("Couldn't see important email: flagged-mail log" in line.text for line in look.lines)
    assert not inputs.important_emails


def test_missing_log_and_unconfigured_google_are_source_failures(tmp_path):
    with pytest.raises(SourceUnavailable, match="log"):
        ImportantMailSource(tmp_path / "missing", None)(NOW, None, ())
    with pytest.raises(SourceUnavailable, match="not configured"):
        ImportantMailSource(write_log(tmp_path, [email()]), None)(NOW, None, ())


def test_failed_unread_check_discards_partial_results_and_still_builds_review(tmp_path):
    def check(message_id):
        if message_id == "fake2":
            raise RuntimeError("fixture failure with private provider detail")
        return True
    inputs = collect_morning_inputs(sources(ImportantMailSource(write_log(tmp_path, [email(), email("fake2")]), check)), (), NOW)
    assert not inputs.important_emails
    assert "private provider detail" not in str(inputs.source_status)
    assert any("Couldn't see important email:" in line.text for line in build_morning(inputs).parts[0].lines)


def test_look_uses_existing_cap_and_emails_have_no_answers(tmp_path):
    calls = []
    source = ImportantMailSource(write_log(tmp_path, [email(f"fake{i:02}") for i in range(15)]),
                                lambda message_id: calls.append(message_id) or True)
    morning = build_morning(collect_morning_inputs(sources(source), (), NOW))
    assert "10 important unread emails." in morning.short
    assert len(calls) == SECTION_CAP
    look = morning.parts[0].lines
    assert len([line for line in look if line.number]) == SECTION_CAP
    assert look[-1].rule == "part_cap"
    rows = [line for line in look if line.rule == "important_email"]
    assert len(rows) == SECTION_CAP - 2  # nothing-timed and Couldn't-see also count
    assert sum("Couldn't see important email: mail check ran out of time" in line.text for line in look) == 1
    assert all(not line.allowed_answers and not line.offered for line in rows)
    assert all(line.record_path is None and line.record_sha256 is None for line in rows)
    assert rows[0].text == "Important email: Sender <sender@example.test> — Fixture subject (reply today)\n" + rows[0].fields_used["link"]


def test_slow_gmail_cannot_hold_delivery_or_check_mail_after_deadline(tmp_path):
    from gremlin_town.runtime import _review_important_emails
    from gremlin_town.sources.google.settings import GoogleSettings
    import httpx

    started, release, finished = Event(), Event(), Event()
    calls, timeouts = [], []

    class SlowGmail(httpx.BaseTransport):
        def handle_request(self, request):
            message_id = request.url.path.rsplit("/", 1)[-1]
            calls.append(message_id)
            timeouts.append(request.extensions["timeout"])
            if message_id == "fake02":
                started.set()
                release.wait(3)
                finished.set()
            return httpx.Response(200, json={"id": message_id, "labelIds": ["UNREAD"]})

    path = write_log(tmp_path, [email(f"fake{i:02}") for i in range(5)])
    settings = ReviewSettings.from_table(dict(enabled=True, why_log_file="fixture.sqlite3",
        important_mail=True, important_mail_log=str(path), important_mail_deadline_seconds=0.2))
    source = _review_important_emails(GoogleSettings(enabled=True, account="fixture@example.test", credential_env="FIXTURE"),
                                    (None, SimpleNamespace(token=lambda: "fixture-token")), settings)
    source.is_unread.__self__._client.close()
    source.is_unread.__self__._client = httpx.Client(transport=SlowGmail())
    ctx = context(tmp_path, source)
    try:
        before = time.monotonic()
        assert run_review(ctx, NOW)["outcome"] == "sent"
        assert time.monotonic() - before < 1
        assert started.is_set() and not finished.is_set()
        packet = ctx.service.commands[-1].packet
        assert packet.context.count("Couldn't see important email: mail check ran out of time") == 1
        shown = [row.item_ref for row in ctx.why_log.morning_history() if row.rule == "important_email"]
        assert shown == [EMAIL_REF_PREFIX + "fake00", EMAIL_REF_PREFIX + "fake01"]
        assert all(timeout["read"] <= 2 for timeout in timeouts)
    finally:
        release.set()
        assert finished.wait(1)
        ctx.why_log.close()
    assert calls == ["fake00", "fake01", "fake02"]


class Delivery:
    def __init__(self):
        self.commands = []
        self.fail = False

    def requests_create(self, actor, command):
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("fixture delivery failed")
        return SimpleNamespace(request_id="fixture-request")


def context(tmp_path, source):
    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    settings = ReviewSettings.from_table(dict(enabled=True, why_log_file=str(path)))
    return ReviewContext(settings, log, sources(source), Delivery())


def test_real_review_logs_email_and_counts_two_mornings_across_restart(tmp_path):
    path = write_log(tmp_path, [email()])
    calls = []
    source = ImportantMailSource(path, lambda message_id: calls.append(message_id) or True)
    ctx = context(tmp_path, source)
    assert run_review(ctx, NOW)["outcome"] == "sent"
    row, = [row for row in ctx.why_log.morning_history() if row.rule == "important_email"]
    reason = ctx.why_log.full_reason(row.item_ref)[0]
    assert reason.fields_used["reason"] == "reply today" and reason.fields_used["score"] == 7
    assert reason.fields_used["link"] == email()["link"]
    assert run_review(ctx, NOW)["outcome"] == "already_sent" and len(calls) == 1
    assert any(line["kind"] == "important_email" for line in current_review_part(ctx, NOW + timedelta(hours=1),
                  part=1, caller="hermes-wa")["lines"])
    ctx.why_log.close()
    log = WhyLog(tmp_path / "why.sqlite3")
    ctx = replace(ctx, why_log=log)
    run_review(ctx, NOW + timedelta(days=1))
    assert "1 important unread email." in ctx.service.commands[-1].packet.constraints["short"]
    run_review(ctx, NOW + timedelta(days=2))
    assert "important unread email" not in ctx.service.commands[-1].packet.constraints["short"]
    assert len([row for row in log.morning_history() if row.rule == "important_email"]) == 2
    log.close()


def test_failed_delivery_frozen_retry_does_not_consume_a_morning(tmp_path):
    calls = []
    ctx = context(tmp_path, ImportantMailSource(write_log(tmp_path, [email()]),
                  lambda message_id: calls.append(message_id) or True))
    ctx.service.fail = True
    with pytest.raises(RuntimeError, match="delivery failed"):
        run_review(ctx, NOW)
    assert ctx.why_log.morning_history() == ()
    frozen = ctx.why_log.morning_packet(today_in_singapore(NOW)).request
    ctx.service.fail = False
    run_review(ctx, NOW + timedelta(minutes=5))
    assert len(calls) == 1 and ctx.why_log.morning_packet(today_in_singapore(NOW)).request == frozen
    assert len([row for row in ctx.why_log.morning_history() if row.rule == "important_email"]) == 1
    ctx.why_log.close()


def test_missing_mail_still_delivers_and_disabled_source_is_not_read(tmp_path):
    ctx = context(tmp_path, ImportantMailSource(tmp_path / "absent", None))
    assert run_review(ctx, NOW)["outcome"] == "sent"
    assert "Couldn't see important email:" in ctx.service.commands[-1].packet.context
    ctx.why_log.close()
    def forbidden(*args):
        pytest.fail("disabled review must not read mail")
    off = replace(ctx, settings=ReviewSettings.from_table(None), sources=sources(forbidden))
    assert run_review(off, NOW) == {"outcome": "disabled"}


def test_runtime_mail_adapter_defaults_off_without_constructing_google_client(tmp_path, monkeypatch):
    from gremlin_town.runtime import _review_important_emails
    from gremlin_town.sources.google.settings import GoogleSettings
    import gremlin_town.sources.google.mail as mail
    def forbidden(*args):
        pytest.fail("off must not construct a mail client")
    monkeypatch.setattr(mail, "MailReader", forbidden)
    on_review = ReviewSettings.from_table(dict(enabled=True, why_log_file="fixture.sqlite3"))
    assert _review_important_emails(GoogleSettings.from_table(None), (None, None), on_review) is None
    assert _review_important_emails(GoogleSettings.from_table(None), None, ReviewSettings.from_table(None)) is None


def test_enabled_runtime_without_google_reports_a_source_failure(tmp_path):
    from gremlin_town.runtime import _review_important_emails
    from gremlin_town.sources.google.settings import GoogleSettings
    path = write_log(tmp_path, [email()])
    settings = ReviewSettings.from_table(dict(enabled=True, why_log_file="fixture.sqlite3",
        important_mail=True, important_mail_log=str(path)))
    source = _review_important_emails(GoogleSettings.from_table(None), None, settings)
    with pytest.raises(SourceUnavailable, match="not configured"):
        source(NOW, None, ())
