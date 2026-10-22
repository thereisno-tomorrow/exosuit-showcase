"""Step I §5a in the asks (build plan slice 6): with `[placement]` off, "where am I?" and "what
next?" answer exactly as before; on, they read today's plan from the why-log. And the poll tells
Nic once when a pick no longer fits today's plan."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from brain_core.placement import Plan, Slot, plan_to_mapping
from brain_core.timewords import SINGAPORE, today_in_singapore

from gremlin_town.review.orient import overview, what_next
from gremlin_town.review.poll import poll_once
from gremlin_town.review.run import ReviewContext, run_moments
from gremlin_town.review.settings import ReviewSettings

from test_orient import NOW, context, lines, make_item, wrap
from test_placement_run import Day, at, item, meeting
from test_poll import FakeEventsService, enabled_settings

TODAY = today_in_singapore(NOW)
DAY_OF = today_in_singapore


def _items():
    urgent = wrap(make_item(id="brn_20261004_120000_u001", title="File the tax form", due="2026-10-04"))
    plain = wrap(make_item(id="brn_20261004_120000_p001", title="Tidy the desk"))
    return urgent, plain


def _record_plan(why_log, *slots: tuple[str, str, str]) -> None:
    def when(clock: str) -> datetime:
        return datetime.combine(TODAY, datetime.strptime(clock, "%H:%M").time(), tzinfo=SINGAPORE)

    plan = Plan(day=TODAY, slots=tuple(Slot(key, "admin", when(start), when(end), "") for key, start, end in slots))
    why_log.record_placement(day=TODAY, ran_at=NOW, fingerprint="f", changes_seen=0,
                             plan=plan_to_mapping(plan, with_text=False), outcome="planned")


def _without_surfacing(answer: dict) -> dict:
    return {key: value for key, value in answer.items() if key != "surfacing_id"}


def test_placement_off_or_with_no_plan_answers_exactly_as_before(tmp_path: Path) -> None:
    urgent, plain = _items()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    off, _ = context(tmp_path=tmp_path / "a", items=(urgent, plain))
    on, _ = context(tmp_path=tmp_path / "b", items=(urgent, plain))
    on = replace(on, placement_enabled=True)
    assert what_next(on, NOW) == what_next(off, NOW)
    assert overview(on, NOW) == overview(off, NOW)
    assert "plan_note" not in what_next(off, NOW)


def test_the_slot_now_leads_what_next_and_names_itself_in_where_am_i(tmp_path: Path) -> None:
    urgent, plain = _items()
    ctx, why_log = context(tmp_path=tmp_path, items=(urgent, plain))
    ctx = replace(ctx, placement_enabled=True)
    _record_plan(why_log, (urgent.record.id, "13:45", "14:15"))
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == urgent.record.id and answer["top_pick"]["rules"][0] == "planned for now"
    assert ("now", "Planned now: ▸ File the tax form until 2:15 pm") in lines(overview(ctx, NOW))


def test_the_plan_note_and_a_passed_slot_with_how_to_move_it(tmp_path: Path) -> None:
    urgent, plain = _items()
    ctx, why_log = context(tmp_path=tmp_path, items=(urgent, plain))
    ctx = replace(ctx, placement_enabled=True)
    _record_plan(why_log, (plain.record.id, "13:00", "13:30"), (urgent.record.id, "13:30", "13:45"))
    answer = what_next(ctx, NOW)
    [asked] = [entry for entry in answer["missed"] if entry["record_id"] == plain.record.id]
    assert asked["move"] == {"kind": "day_change", "detail": '{"move": "%s"}' % plain.record.id}
    assert "planned for 1 pm–1:30 pm" in asked["question"]
    assert "plan_note" not in answer  # no slot now

    (tmp_path / "second").mkdir()
    ctx2, why_log2 = context(tmp_path=tmp_path / "second", items=(urgent, plain))
    ctx2 = replace(ctx2, placement_enabled=True)
    _record_plan(why_log2, (plain.record.id, "13:45", "14:15"))
    noted = what_next(ctx2, NOW)
    assert noted["top_pick"]["record_id"] == urgent.record.id
    assert noted["plan_note"].startswith('The plan says "Tidy the desk" until 2:15 pm, but "File the tax form" comes first')


def _review_day(tmp_path: Path, **settings):
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    service = FakeEventsService({})
    table = {"enabled": True, "sender_participant": "exosuit-review", "delivery_participant": "hermes-wa",
             "project": "vault_home", "why_log_file": str(tmp_path / "why.sqlite3"), **settings}
    review = ReviewContext(
        settings=ReviewSettings.from_table(table), why_log=day.why_log, sources=day.context.sources,
        service=service, placement=day.context,
    )
    return bank, day, service, review


def test_a_pick_that_stops_fitting_waits_for_the_daytime_return_and_is_told_once(tmp_path: Path) -> None:
    bank, day, service, review = _review_day(tmp_path)
    poll_once(review, at("06:32"))
    day.events.append(meeting("09:00", "18:00"))  # the whole window taken: the pick no longer fits
    [placed] = [entry["placement"] for entry in poll_once(review, at("06:40")) if "placement" in entry]
    assert placed["no_longer_fits"] == [{"key": bank.record.id, "title": "bank"}]
    assert service.created == []  # never a push of its own
    assert day.why_log.pending_placement_notices(DAY_OF(at("06:40"))) == ({"key": bank.record.id, "title": "bank"},)
    returned = run_moments(review, at("08:00"), "pc_on", signal_id="pc-1")
    assert returned["outcome"] == "sent" and returned["placement_notices"] == (bank.record.id,)
    [sent] = service.created
    assert sent.packet.context == '1. "bank" no longer fits today\'s plan. Drop it, move it to tomorrow, or swap it for something?'
    assert day.why_log.pending_placement_notices(DAY_OF(at("08:00"))) == ()
    day.events.append(meeting("18:00", "18:30"))  # the run goes again; the notice was already told today
    poll_once(review, at("08:10"))
    assert run_moments(review, at("09:00"), "pc_on", signal_id="pc-2")["outcome"] == "nothing"


def test_with_the_courier_the_notice_is_posted_once(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    bank, day, service, review = _review_day(tmp_path, telegram_spool_dir=str(spool))
    poll_once(review, at("06:32"))
    day.events.append(meeting("09:00", "18:00"))
    poll_once(review, at("06:40"))
    run_moments(review, at("08:00"), "pc_on", signal_id="pc-1")
    [sent] = service.created
    assert "courier" in sent.packet.objective.lower() and "verbatim" not in sent.packet.next_action.lower()
    assert len(list(spool.iterdir())) == 1


def test_a_failed_daytime_return_keeps_the_notice(tmp_path: Path) -> None:
    bank, day, service, review = _review_day(tmp_path)
    poll_once(review, at("06:32"))
    day.events.append(meeting("09:00", "18:00"))
    poll_once(review, at("06:40"))

    def broken(actor, command):
        raise RuntimeError("hub down")

    service.requests_create = broken
    try:
        run_moments(review, at("08:00"), "pc_on", signal_id="pc-1")
    except RuntimeError:
        pass
    assert day.why_log.pending_placement_notices(DAY_OF(at("08:00")))


def test_a_placement_failure_never_escapes_the_poll(tmp_path: Path) -> None:
    bank, day, service, review = _review_day(tmp_path)
    day.context.sources = replace(day.context.sources, brain_items=lambda: (_ for _ in ()).throw(ZeroDivisionError("oops")))
    [placed] = [entry["placement"] for entry in poll_once(review, at("06:32")) if "placement" in entry]
    assert placed["outcome"] == "error" and "oops" in placed["error"]


def test_a_moved_task_with_no_room_is_not_asked_again(tmp_path: Path) -> None:
    urgent, plain = _items()
    ctx, why_log = context(tmp_path=tmp_path, items=(urgent, plain))
    ctx = replace(ctx, placement_enabled=True)
    _record_plan(why_log, (plain.record.id, "13:00", "13:30"))
    why_log.record_signal(kind="day_change", actor="hermes-wa", at=NOW - timedelta(minutes=5),
                          detail='{"move": "%s"}' % plain.record.id)
    assert "missed" not in what_next(ctx, NOW)


def test_a_bad_plan_row_never_blocks_an_answer(tmp_path: Path) -> None:
    urgent, plain = _items()
    ctx, why_log = context(tmp_path=tmp_path, items=(urgent, plain))
    ctx = replace(ctx, placement_enabled=True)
    why_log.record_placement(day=TODAY, ran_at=NOW, fingerprint="f", changes_seen=0, plan={"day": "not a day"},
                             outcome="planned")
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == urgent.record.id
    assert any("placement" in entry for entry in answer["unknowns"])


def test_a_superseded_versions_title_never_wins(tmp_path: Path) -> None:
    old = wrap(make_item(id="brn_20261004_110000_x001", title="Old title"))
    new_record = replace(make_item(id="brn_20261004_120000_x002", title="New title"), supersedes=(old.record.id,))
    new = wrap(new_record)
    ctx, why_log = context(tmp_path=tmp_path, items=(old, new))
    ctx = replace(ctx, placement_enabled=True)
    _record_plan(why_log, (old.record.id, "13:45", "14:15"))
    assert ("now", "Planned now: \u25b8 New title until 2:15 pm") in lines(overview(ctx, NOW))
