from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from brain_core.review import HistoryRow
from gremlin_town.review.schedule import late_notices
from test_orient import NOW, make_item, wrap
from test_orient import context as orientation_context
from test_poll import FakeEventsService
from gremlin_town.review.run import ReviewContext, run_moments
import pytest


def late():
    record = make_item(id="brn_schedule_late", title="Send the form")
    record = replace(record, data={"level": "accepted", "dates": {"deadline": {"day": "2026-10-04", "at": "15:00"}}})
    snapshot = SimpleNamespace(available=True, blocks=(SimpleNamespace(
        item_ref=record.id, end=NOW + timedelta(hours=2)),))
    return record, snapshot


def test_notice_uses_fresh_title_and_current_deadline():
    record, snapshot = late()
    [notice] = late_notices(snapshot, (wrap(record),), (), NOW)
    assert notice["title"] == "Send the form"
    relaxed = replace(record, data={"level": "accepted", "dates": {"deadline": {"day": "2026-10-05"}}})
    assert late_notices(snapshot, (wrap(relaxed),), (), NOW) == ()


def test_unavailable_snapshot_or_finished_work_never_returns_a_late_notice():
    record, snapshot = late()
    assert late_notices(SimpleNamespace(available=False), (wrap(record),), (), NOW) == ()
    for state in ("done", "dropped", "parked"):
        assert late_notices(snapshot, (wrap(replace(record, state=state)),), (), NOW) == ()


def test_notice_is_once_per_day_after_successful_delivery():
    record, snapshot = late()
    told = HistoryRow(item_ref=record.id, channel="moment", rule="scheduled_after_deadline",
                      record_sha256="test", record_state="open", surfaced_at=NOW)
    assert late_notices(snapshot, (wrap(record),), (told,), NOW) == ()
    assert len(late_notices(snapshot, (wrap(record),), (replace(told, surfaced_at=NOW-timedelta(days=1)),), NOW)) == 1


def test_daytime_delivery_reports_late_schedule_once_and_retries_failed_send(tmp_path):
    record, snapshot = late()
    ctx, log = orientation_context(tmp_path=tmp_path, items=(wrap(record),))
    service = FakeEventsService({})
    review = ReviewContext(settings=ctx.review_settings, why_log=log,
                           sources=ctx.sources.review, service=service,
                           schedule_enabled=True, schedule=lambda now: snapshot)
    original = service.requests_create
    def failed(actor, command):
        raise RuntimeError("fake delivery failure")
    service.requests_create = failed
    with pytest.raises(RuntimeError, match="fake delivery"):
        run_moments(review, NOW, "pc_on", signal_id="fixture-one")
    assert not any(row.rule == "scheduled_after_deadline" for row in log.history())
    service.requests_create = original
    answer = run_moments(review, NOW, "pc_on", signal_id="fixture-one")
    assert answer["schedule_notices"] == (record.id,)
    assert '"Send the form" is now scheduled after its deadline.' in service.created[-1].packet.context
    answer = run_moments(review, NOW + timedelta(minutes=5), "pc_on", signal_id="fixture-two")
    assert "schedule_notices" not in answer


def test_late_notice_uses_current_version_evidence_regardless_of_scan_order():
    old, snapshot = late()
    current = replace(old, id="brn_schedule_late_new", supersedes=(old.id,), title="Fresh title")
    old_entry = replace(wrap(old), path="old.md")
    new_entry = replace(wrap(current), path="current.md")
    [notice] = late_notices(snapshot, (new_entry, old_entry), (), NOW)
    assert (notice["title"], notice["path"], notice["sha256"]) == ("Fresh title", "current.md", new_entry.sha256)


def test_late_notice_honors_fresh_ancestor_person_wait():
    child, snapshot = late()
    parent = replace(make_item(id="brn_schedule_parent", title="Office work"),
        data={"level": "accepted", "called": "project", "needs": {"from": "Synthetic colleague", "what": "Response"}})
    child = replace(child, data={**child.data, "parent": parent.id})
    assert late_notices(snapshot, (wrap(parent), wrap(child)), (), NOW) == ()


def test_overlapping_signals_deliver_late_notice_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    record, snapshot = late()
    ctx, log = orientation_context(tmp_path=tmp_path, items=(wrap(record),))
    service = FakeEventsService({})
    entered, release = threading.Event(), threading.Event()
    counter = []
    def read(now):
        counter.append(True)
        if len(counter) == 1:
            entered.set()
            assert release.wait(10)
        return snapshot
    review = ReviewContext(settings=ctx.review_settings, why_log=log, sources=ctx.sources.review,
        service=service, schedule_enabled=True, schedule=read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_moments, review, NOW, "pc_on", signal_id="first")
        assert entered.wait(10)
        try:
            second = pool.submit(run_moments, review, NOW, "pc_on", signal_id="second")
            second.result(timeout=10)
        finally:
            release.set()
        first.result(timeout=10)
    assert len(service.created) == 1


def test_connector_mode_suppresses_old_notices_even_if_reader_is_off(tmp_path):
    ctx, log = orientation_context(tmp_path=tmp_path, items=())
    log.note_placement_notice(day=NOW.date(), key="synthetic-old", title="Old placement", noted_at=NOW)
    service = FakeEventsService({})
    review = ReviewContext(settings=ctx.review_settings, why_log=log, sources=ctx.sources.review,
        service=service, flowsavvy_enabled=True)
    result = run_moments(review, NOW, "pc_on", signal_id="fixture-switch")
    assert "placement_notices" not in result and service.created == []


def test_schedule_return_lock_coordinates_separate_log_connections(tmp_path):
    from gremlin_town.review.why_log import WhyLog
    first = WhyLog(tmp_path / "shared.db")
    second = WhyLog(tmp_path / "shared.db")
    try:
        with first.schedule_return_lock() as held:
            assert held
            with second.schedule_return_lock() as other:
                assert not other
        with second.schedule_return_lock() as after:
            assert after
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("kind", ["pc_on", "task_finished", "calendar_gap"])
def test_busy_schedule_return_keeps_signal_for_next_poll(tmp_path, kind):
    from gremlin_town.review.poll import _poll_pc_on, _poll_task_finished, _poll_calendar_gap
    record, snapshot = late()
    ctx, log = orientation_context(tmp_path=tmp_path, items=(wrap(record),))
    service = FakeEventsService({"result_returned": ({"sequence": 1},)})
    sources = replace(ctx.sources.review, calendar_events=lambda start, end: ())
    review = ReviewContext(settings=ctx.review_settings, why_log=log, sources=sources,
        service=service, schedule_enabled=True, schedule=lambda now: snapshot)
    log.record_signal(kind="pc_on", actor="local-codex", at=NOW)
    poll = {"pc_on": _poll_pc_on, "task_finished": _poll_task_finished, "calendar_gap": _poll_calendar_gap}[kind]
    with log.schedule_return_lock():
        [result] = poll(review, NOW)
        assert result["outcome"] == "busy"
    assert log.latest_cursor("moments_poll_" + kind) is None
    [retried] = poll(review, NOW)
    assert retried["outcome"] == "sent"
    assert log.latest_cursor("moments_poll_" + kind) is not None
