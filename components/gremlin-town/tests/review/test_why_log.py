from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gremlin_town.review.why_log import PreparedMorning, Surfacing, WhyLog, WhyLogError

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


def make_log(tmp_path: Path) -> WhyLog:
    return WhyLog(tmp_path / "why" / "why.sqlite3")


def test_bootstrap_is_explicit_immutable_and_creates_no_review_history(tmp_path):
    log = make_log(tmp_path)
    assert log.morning_bootstrap() is None and not log.morning_bootstrap_completed()
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_old", "brn_old", "brn_other"))
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_other", "brn_old"))
    assert log.morning_bootstrap().known_first_ids == frozenset(("brn_old", "brn_other"))
    assert log.history() == log.morning_history() == () and log.last_sent_review_at() is None
    assert log.shown_item_refs() == frozenset()
    for sha, ids in (("b" * 40, ("brn_old", "brn_other")), ("a" * 40, ("brn_old",))):
        with pytest.raises(ValueError, match="already pinned"):
            log.stage_morning_bootstrap(input_sha=sha, first_ids=ids)
    with pytest.raises(ValueError, match="input_sha"):
        log.stage_morning_bootstrap(input_sha="guess", first_ids=())
    with pytest.raises(ValueError, match="inventory"):
        log.stage_morning_bootstrap(input_sha="a" * 40, first_ids="brn_old")
    log.close()


def test_only_successful_new_morning_consumes_bootstrap_and_inventory_survives_prune(tmp_path):
    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_old",))
    log.record_surfacings([surfacing("brn_new")])
    for kind, outcome in (("review", "failed"), ("review", "aborted"), ("weekly", "answered"), ("moments", "pc_on:sent")):
        log.record_run(kind=kind, started_at=NOW, last_event_cursor=None, outcome=outcome,
                       bootstrap_input_sha="a" * 40)
        assert not log.morning_bootstrap_completed()
    assert log.shown_item_refs() == frozenset()
    assert log.morning_history() == ()
    log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                   bootstrap_input_sha="a" * 40)
    assert log.morning_bootstrap_completed()
    assert log.shown_item_refs() == frozenset(("brn_new",))
    assert len(log.morning_history()) == 1
    log.prune(NOW + timedelta(days=98))
    log.close()
    log = WhyLog(path)
    assert log.history() == ()
    assert log.shown_item_refs() == frozenset(("brn_new",))
    facts = log.morning_bootstrap()
    assert facts.completed and facts.known_first_ids == frozenset(("brn_old",))
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_old",))
    assert log.morning_bootstrap_completed()  # same staging cannot reset completion
    log.close()


def test_old_sent_runs_backfill_shown_refs_but_never_consume_first_style_bootstrap(tmp_path):
    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.record_surfacings([surfacing("brn_old_physical"), surfacing("brn_failed", surfaced_at=NOW + timedelta(hours=1)),
        surfacing("brn_overview", channel="overview")])
    log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent")
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    assert not log.morning_bootstrap_completed()
    # Model upgrading an existing log whose durable shown inventory did not exist.
    with log._connection:
        log._connection.execute("DELETE FROM shown_item_refs")
    log.close()
    log = WhyLog(path)
    assert log.shown_item_refs() == frozenset(("brn_old_physical", "brn_overview"))
    assert [row.item_ref for row in log.morning_history() if row.channel == "review"] == ["brn_old_physical"]
    log.close()


def test_shown_inventory_includes_successful_weekly_and_moments_without_bootstrap_consumption(tmp_path):
    log = make_log(tmp_path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    log.record_surfacings([surfacing("brn_weekly", channel="weekly"),
        surfacing("brn_moment", channel="moment", surfaced_at=NOW + timedelta(seconds=1)),
        surfacing("brn_failed_moment", channel="moment", surfaced_at=NOW + timedelta(seconds=2))])
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    log.record_run(kind="moments", started_at=NOW + timedelta(seconds=1), last_event_cursor=None,
                   outcome="pc_on:sent:couldnt_read")
    log.record_run(kind="moments", started_at=NOW + timedelta(seconds=2), last_event_cursor=None,
                   outcome="pc_on:sent_failed")
    assert log.shown_item_refs() == frozenset(("brn_weekly", "brn_moment"))
    assert not log.morning_bootstrap_completed() and log.last_sent_review_at() is None
    log.prune(NOW + timedelta(days=98))
    assert log.shown_item_refs() == frozenset(("brn_weekly", "brn_moment"))
    log.close()


def test_bootstrap_completion_rolls_back_with_failed_run_and_request_marker_transaction(tmp_path):
    log = make_log(tmp_path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_old",))
    log.record_surfacings([surfacing("req_proof", rule="request_returned_proof")])
    with log._connection:
        log._connection.execute("""CREATE TRIGGER fail_marker BEFORE INSERT ON request_review_consumption
            BEGIN SELECT RAISE(ABORT, 'fixture marker failure'); END""")
    with pytest.raises(WhyLogError):
        log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                       bootstrap_input_sha="a" * 40)
    assert not log.morning_bootstrap_completed() and log.last_sent_review_at() is None
    assert log.shown_item_refs() == frozenset() and log.morning_history() == ()
    with log._connection:
        log._connection.execute("DROP TRIGGER fail_marker")
    with pytest.raises(WhyLogError, match="match"):
        log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                       bootstrap_input_sha="b" * 40)
    assert not log.morning_bootstrap_completed() and log.last_sent_review_at() is None
    log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                   bootstrap_input_sha="a" * 40)
    assert log.morning_bootstrap_completed() and len(log.request_review_history()) == 1
    log.close()


def prepared(log, *, bootstrap_sha="a" * 40):
    refs = log.review_references(NOW)
    return PreparedMorning(NOW, {"client_operation_id": "review-2026-09-26", "text": "Original full parts",
        "constraints": {"lines": [{"reference": ref} for ref in refs]}}, refs, "Original short",
        bootstrap_sha, False)


def test_prepared_packet_freezes_first_facts_across_restart_without_claiming_sent(tmp_path):
    from dataclasses import replace
    from brain_core.timewords import today_in_singapore

    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.record_surfacings([surfacing(question="Original question", offered=("parts",))])
    first = log.prepare_morning(prepared(log))
    later = replace(first, prepared_at=NOW + timedelta(minutes=5), short="Changed short",
                    request={**first.request, "text": "New capture would change the full parts"})
    assert log.prepare_morning(later) == first
    assert log.last_sent_review_at() is None and log.morning_history() == ()
    assert log.shown_item_refs() == frozenset()
    log.close()
    log = WhyLog(path)
    assert log.morning_packet(today_in_singapore(NOW)) == first
    assert not log.morning_packet(today_in_singapore(NOW)).completed
    assert log.morning_packet(today_in_singapore(NOW + timedelta(days=1))) is None
    log.close()
    with pytest.raises(WhyLogError):
        log.morning_packet(today_in_singapore(NOW))


def test_packet_preparation_rejects_unlogged_refs_and_fake_completion(tmp_path):
    from dataclasses import replace

    log = make_log(tmp_path)
    packet = prepared(log)
    with pytest.raises(WhyLogError, match="already logged"):
        log.prepare_morning(replace(packet, references=("123",)))
    with pytest.raises(WhyLogError, match="no successful completion"):
        log.prepare_morning(replace(packet, completed=True))
    assert log.last_sent_review_at() is None and log.history() == ()
    log.close()


def test_packet_success_is_atomic_and_consumes_only_its_exact_refs_after_restart(tmp_path):
    from brain_core.timewords import today_in_singapore

    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=("brn_known",))
    log.record_surfacings([surfacing("req_real", rule="request_returned_proof", question="Shown proof")])
    packet = log.prepare_morning(prepared(log))
    # An abandoned concurrent batch at the same timestamp is not part of this
    # request. Its rows must not become shown, carried, or consumed on restart.
    log.record_surfacings([surfacing("req_abandoned", rule="request_returned_proof"),
                          surfacing("brn_abandoned", rule="carried_over")])
    for kind, outcome in (("review", "aborted"), ("weekly", "answered")):
        log.record_run(kind=kind, started_at=NOW, last_event_cursor=None, outcome=outcome)
    assert not log.morning_packet(today_in_singapore(NOW)).completed
    assert not log.morning_bootstrap_completed() and log.shown_item_refs() == frozenset()
    with log._connection:
        log._connection.execute("""CREATE TRIGGER fail_packet_marker BEFORE INSERT ON request_review_consumption
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    with pytest.raises(WhyLogError):
        log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                       bootstrap_input_sha=packet.bootstrap_input_sha)
    assert not log.morning_packet(today_in_singapore(NOW)).completed
    assert not log.morning_bootstrap_completed() and log.last_sent_review_at() is None
    with log._connection:
        log._connection.execute("DROP TRIGGER fail_packet_marker")
    with pytest.raises(WhyLogError, match="exact prepared packet"):
        log.record_run(kind="review", started_at=NOW + timedelta(minutes=5), last_event_cursor=None, outcome="sent",
                       bootstrap_input_sha=packet.bootstrap_input_sha)
    assert log.last_sent_review_at() is None
    log.record_run(kind="review", started_at=NOW, last_event_cursor=None, outcome="sent",
                   bootstrap_input_sha=packet.bootstrap_input_sha)
    assert log.morning_packet(today_in_singapore(NOW)).completed and log.morning_bootstrap_completed()
    assert log.shown_item_refs() == frozenset(("req_real",))
    assert {row.item_ref for row in log.morning_history()} == {"req_real"}
    with log._connection:
        log._connection.execute("DELETE FROM request_review_consumption")
    log.close()
    log = WhyLog(path)
    assert {row.item_ref for row in log.request_review_history() if row.rule == "request_returned_proof"} == {"req_real"}
    assert log.shown_item_refs() == frozenset(("req_real",))
    log.prune(NOW + timedelta(days=98))
    assert log.shown_item_refs() == frozenset(("req_real",))
    assert {row.item_ref for row in log.morning_history()} == {"req_real"}
    assert log.morning_packet(today_in_singapore(NOW)) is None
    assert log.morning_bootstrap_completed()  # derivative identity facts remain durable
    log.close()


def surfacing(item_ref: str = "brn_bill", **overrides: object) -> Surfacing:
    values = dict(
        surfaced_at=NOW,
        channel="review",
        item_ref=item_ref,
        record_sha256="a" * 64,
        rule="missed",
        fields_used={"due": "2026-09-24"},
        record_state="open",
        assumptions=(),
        unknowns=(),
    )
    values.update(overrides)
    return Surfacing(**values)  # type: ignore[arg-type]


def test_creates_parent_directory_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "why.sqlite3"
    log = WhyLog(path)
    assert path.exists()
    log.close()


CALENDAR_KEY = dict(owner_locator="vault://brain-home", owner_id="brain-home", caller="hermes-wa",
    operation_id="work:appointment:brn_20260926_060000_1234567890abcdef",
    operation_hash="a"*64, brain_commit="b"*40)
CALENDAR_ACK = {"status":"moved", "text":"Moved on your calendar to Fri 2 Oct 7 pm, 30 min, under your calendar limit",
    "start":"2026-10-02T19:00:00+08:00", "end":"2026-10-02T19:30:00+08:00", "all_day":False, "minutes":30}


def test_calendar_acknowledgment_is_exact_durable_and_not_pruned(tmp_path):
    path=tmp_path/'why.sqlite3'
    log=WhyLog(path)
    assert log.calendar_effect(**CALENDAR_KEY) is None
    assert log.record_calendar_effect(**CALENDAR_KEY,calendar=CALENDAR_ACK)==CALENDAR_ACK
    assert log.record_calendar_effect(**CALENDAR_KEY,calendar=CALENDAR_ACK)==CALENDAR_ACK
    log.record_surfacings([surfacing()])
    assert log.prune(NOW+timedelta(days=100))==1
    log.close()
    reopened=WhyLog(path)
    observed=reopened.calendar_effect(**CALENDAR_KEY)
    assert observed==CALENDAR_ACK
    observed['text']='a changed returned copy'
    assert reopened.calendar_effect(**CALENDAR_KEY)==CALENDAR_ACK
    with pytest.raises(WhyLogError):
        reopened.record_calendar_effect(**CALENDAR_KEY,calendar={**CALENDAR_ACK,'text':'another acknowledgment'})
    assert reopened.calendar_effect(**CALENDAR_KEY)==CALENDAR_ACK
    reopened.close()


@pytest.mark.parametrize('field,value',[('owner_locator','vault://other'),('owner_id','other'),('caller','local-codex'),
    ('operation_id','work:another:record'),('operation_hash','c'*64),('brain_commit','d'*40)])
def test_calendar_acknowledgment_is_scoped_by_all_verified_operation_keys(tmp_path,field,value):
    log=make_log(tmp_path)
    log.record_calendar_effect(**CALENDAR_KEY,calendar=CALENDAR_ACK)
    assert log.calendar_effect(**{**CALENDAR_KEY,field:value}) is None
    log.close()


@pytest.mark.parametrize('status',['failed','has_guests','skipped'])
def test_calendar_failures_and_blocked_effects_are_not_finalized(tmp_path,status):
    log=make_log(tmp_path)
    with pytest.raises(WhyLogError):
        log.record_calendar_effect(**CALENDAR_KEY,calendar={'status':status,'text':'not a successful acknowledgment'})
    assert log.calendar_effect(**CALENDAR_KEY) is None
    log.close()


def test_answer_context_keeps_question_offers_and_only_today_is_marked(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    for when in (NOW - timedelta(days=1), NOW):
        log.record_surfacings([surfacing(item_ref="brn_first", surfaced_at=when,
            question="Same as 'Board for Workshop'? Merge?", offered=("merge",),
            fields_used={"review_marks": ["maybe_same"]})])
    yesterday, today = log.answer_context("1"), log.answer_context("2")
    assert yesterday.open and today.open
    assert today.question == "Same as 'Board for Workshop'? Merge?"
    assert today.offered == ("merge",) and today.thing == "brn_first"
    assert today.marks == ("maybe_same",)
    log.mark_today_answered(["brn_first"], message="yes", now=NOW)
    assert log.answer_context("1").open
    assert not log.answer_context("2").open
    assert log.answer_context("unknown") is None
    assert log.answer_context("999") is None
    log.close()


def test_record_and_read_history_round_trips_fields(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([surfacing(rule="missed", fields_used={"due": "2026-09-24"}, unknowns=("no reply yet",))])
    history = log.history()
    assert len(history) == 1
    row = history[0]
    assert row.item_ref == "brn_bill"
    assert row.rule == "missed"
    assert row.record_state == "open"
    assert row.response is None


def test_write_before_send_pattern_fails_loudly_on_bad_parent(tmp_path: Path) -> None:
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(WhyLogError):
        WhyLog(blocking_file / "why.sqlite3")


def test_record_response_updates_most_recent_surfacing(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([surfacing(item_ref="brn_x", surfaced_at=NOW - timedelta(hours=2))])
    log.record_surfacings([surfacing(item_ref="brn_x", surfaced_at=NOW)])
    log.record_response(item_ref="brn_x", response="picked_today", response_words="pick it", responded_at=NOW)
    history = log.history()
    responded = [row for row in history if row.response == "picked_today"]
    assert len(responded) == 1
    assert responded[0].surfaced_at == NOW


def test_record_response_without_prior_surfacing_raises(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with pytest.raises(WhyLogError):
        log.record_response(item_ref="brn_missing", response="picked_today", response_words=None, responded_at=NOW)


def test_full_reason_returns_every_column(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([
        surfacing(
            item_ref="brn_full",
            fields_used={"due": "2026-09-24"},
            assumptions=("assumed Singapore time",),
            unknowns=("no reply yet",),
        )
    ])
    reasons = log.full_reason("brn_full")
    assert len(reasons) == 1
    reason = reasons[0]
    assert reason.fields_used == {"due": "2026-09-24"}
    assert reason.assumptions == ("assumed Singapore time",)
    assert reason.unknowns == ("no reply yet",)


def test_signals_and_runs_are_recorded(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_signal(kind="pc_on", actor="local-codex", at=NOW, detail=None)
    log.record_run(kind="review", started_at=NOW, last_event_cursor="42", outcome="sent")
    rows = log._connection.execute("SELECT * FROM signals").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "pc_on"
    runs = log._connection.execute("SELECT * FROM runs").fetchall()
    assert runs[0]["outcome"] == "sent"


def test_prune_removes_rows_older_than_90_days_keeps_89(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([surfacing(item_ref="brn_old", surfaced_at=NOW - timedelta(days=91))])
    log.record_surfacings([surfacing(item_ref="brn_recent", surfaced_at=NOW - timedelta(days=89))])
    removed = log.prune(NOW)
    assert removed == 1
    remaining = {row.item_ref for row in log.history()}
    assert remaining == {"brn_recent"}


def test_empty_surfacings_write_is_a_no_op(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([])
    assert log.history() == ()


def test_signals_since_returns_only_newer_rows_in_order(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_signal(kind="pc_on", actor="local-codex", at=NOW, detail=None)
    log.record_signal(kind="pc_on", actor="local-codex", at=NOW + timedelta(minutes=1), detail=None)
    first_two = log.signals_since(0)
    assert [row.id for row in first_two] == [1, 2]
    assert log.signals_since(first_two[0].id) == (first_two[1],)
    assert log.signals_since(first_two[-1].id) == ()


def test_latest_cursor_is_none_until_a_run_records_one(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    assert log.latest_cursor("moments_poll_pc_on") is None
    log.record_run(kind="moments_poll_pc_on", started_at=NOW, last_event_cursor="5", outcome="checked")
    assert log.latest_cursor("moments_poll_pc_on") == "5"
    log.record_run(kind="moments_poll_pc_on", started_at=NOW, last_event_cursor="9", outcome="checked")
    assert log.latest_cursor("moments_poll_pc_on") == "9"
    # A run with no cursor (nothing new) never regresses the latest one.
    log.record_run(kind="moments_poll_pc_on", started_at=NOW, last_event_cursor=None, outcome="checked")
    assert log.latest_cursor("moments_poll_pc_on") == "9"


def test_weekly_success_consumes_only_logged_stale_at_exact_packet_time_channel(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([
        surfacing("weekly-stale", channel="weekly", rule="stale_request"),
        surfacing("other-time", channel="weekly", rule="stale_request", surfaced_at=NOW - timedelta(seconds=1)),
        surfacing("morning-stale", rule="stale_request"),
        surfacing("weekly-proof", channel="weekly", rule="request_returned_proof"),
    ])
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    markers = log._connection.execute("SELECT item_ref FROM request_review_consumption").fetchall()
    assert [row["item_ref"] for row in markers] == ["weekly-stale"]
    runs = log._connection.execute("SELECT kind, outcome FROM runs").fetchall()
    assert [(row["kind"], row["outcome"]) for row in runs] == [("weekly", "answered")]
    log.close()


@pytest.mark.parametrize("outcome", ["failed", "aborted"])
def test_weekly_failed_or_aborted_packet_consumes_nothing_and_can_retry(tmp_path: Path, outcome: str) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([surfacing("stale", channel="weekly", rule="stale_request")])
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome=outcome, consume_channel="weekly")
    assert log.request_review_history() == ()
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    assert [(row.item_ref, row.rule) for row in log.request_review_history()] == [("stale", "stale_request")]
    log.close()


def test_weekly_run_and_consumption_roll_back_together_on_marker_write_failure(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_surfacings([surfacing("stale", channel="weekly", rule="stale_request")])
    log._connection.execute("""CREATE TRIGGER fail_weekly BEFORE INSERT ON request_review_consumption
                               BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
    with pytest.raises(WhyLogError, match="run row"):
        log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    assert log._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert log.request_review_history() == ()
    log._connection.execute("DROP TRIGGER fail_weekly")
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    assert [row.item_ref for row in log.request_review_history()] == ["stale"]
    log.close()


def test_weekly_consumption_survives_restart_and_prune_with_morning_rules_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "why.sqlite3"
    log = WhyLog(path)
    old = NOW - timedelta(days=100)
    log.record_surfacings([surfacing("weekly", channel="weekly", rule="stale_request", surfaced_at=old),
                          surfacing("morning-stale", rule="stale_request", surfaced_at=old),
                          surfacing("morning-proof", rule="request_returned_proof", surfaced_at=old)])
    log.record_run(kind="weekly", started_at=old, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    assert {row.item_ref for row in log.request_review_history()} == {"weekly"}
    log.record_run(kind="review", started_at=old, last_event_cursor=None, outcome="failed")
    assert {row.item_ref for row in log.request_review_history()} == {"weekly"}
    log.record_run(kind="review", started_at=old, last_event_cursor=None, outcome="sent")
    log.close()
    reopened = WhyLog(path)
    assert reopened.prune(NOW) == 3
    assert reopened.history() == ()
    assert {(row.item_ref, row.rule) for row in reopened.request_review_history()} == {
        ("weekly", "stale_request"), ("morning-stale", "stale_request"), ("morning-proof", "request_returned_proof"),
    }
    reopened.close()


def test_weekly_consumption_with_no_logged_line_does_not_invent_a_marker(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record_run(kind="weekly", started_at=NOW, last_event_cursor=None, outcome="answered", consume_channel="weekly")
    assert log.request_review_history() == ()
    log.close()
