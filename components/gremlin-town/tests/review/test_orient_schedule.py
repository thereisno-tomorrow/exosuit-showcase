"""Orientation reads one local snapshot and keeps Brain Home authoritative."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from test_orient import NOW, context, make_item, wrap
from gremlin_town.review.orient import overview, what_next


def snapshot(record, *, available=True):
    return SimpleNamespace(
        available=available, fetched_at=NOW - timedelta(minutes=2), age_seconds=120,
        blocks=(SimpleNamespace(item_ref=record.id, title="obsolete remote title",
                                start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=25)),),
    )


def test_disabled_schedule_never_reads_snapshot(tmp_path):
    record = make_item(id="brn_schedule_off", title="Tidy the desk")
    ctx, _ = context(tmp_path=tmp_path, items=(wrap(record),))
    def unexpected(now):
        raise AssertionError("disabled schedule was read")
    changed = replace(ctx, sources=replace(ctx.sources, schedule=unexpected))
    assert what_next(changed, NOW) == what_next(ctx, NOW)


def test_enabled_schedule_reads_once_and_joins_fresh_title_and_age(tmp_path):
    record = make_item(id="brn_schedule_join", title="Tidy the desk")
    ctx, _ = context(tmp_path=tmp_path, items=(wrap(record),))
    calls = []
    def read(now):
        calls.append(now)
        return snapshot(record)
    ctx = replace(ctx, schedule_enabled=True, sources=replace(ctx.sources, schedule=read))
    answer = what_next(ctx, NOW)
    assert calls == [NOW]
    assert answer["top_pick"]["title"] == "Tidy the desk"
    assert answer["top_pick"]["rules"][0] == "scheduled for now"
    assert answer["schedule"] == {"available": True, "updated_at": (NOW - timedelta(minutes=2)).isoformat(), "age_seconds": 120}


def test_unavailable_schedule_falls_back_without_using_last_blocks(tmp_path):
    record = make_item(id="brn_schedule_stale", title="Tidy the desk")
    ctx, _ = context(tmp_path=tmp_path, items=(wrap(record),))
    ctx = replace(ctx, schedule_enabled=True,
                  sources=replace(ctx.sources, schedule=lambda now: snapshot(record, available=False)))
    answer = what_next(ctx, NOW)
    assert answer["top_pick"]["record_id"] == record.id
    assert "scheduled for now" not in answer["top_pick"]["rules"]
    assert answer["plan_note"] == "Couldn't see the schedule"
    assert answer["schedule"]["available"] is False


def test_failed_schedule_read_is_sanitized_and_overview_still_answers(tmp_path):
    record = make_item(id="brn_schedule_error", title="Tidy the desk")
    ctx, _ = context(tmp_path=tmp_path, items=(wrap(record),))
    def failed(now):
        raise OSError("synthetic private diagnostic must not escape")
    ctx = replace(ctx, schedule_enabled=True, sources=replace(ctx.sources, schedule=failed))
    answer = overview(ctx, NOW)
    assert "Couldn't see the schedule" in str(answer)
    assert "private diagnostic" not in str(answer)
    assert answer["schedule"]["available"] is False
