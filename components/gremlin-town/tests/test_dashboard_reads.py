"""Fake records only. No provider, network, model, live keys or Brain Home writes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from brain_core import work
from brain_core.dashboard import project_work
from brain_core.records import Record
from brain_core.review import CalendarEvent, ItemRecord
from gremlin_town.dashboard.reads import DashboardReader, DashboardSources, DashboardUnavailable
from gremlin_town.dashboard.read_routes import create_read_router
from gremlin_town.flowsavvy.snapshot import ScheduleBlock, ScheduleSnapshot

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 2, 11, 20, tzinfo=SGT)


def thing(number, title="Test task", *, state="open", body="A fake note", **data):
    record = Record(schema="brain.record/v2", id=f"brn_20261002_010000_{number:016x}",
                    kind="item", title=title, state=state, created_at=NOW - timedelta(hours=3),
                    updated_at=NOW - timedelta(hours=3), assertion="explicit", confidence=None,
                    sensitivity="private", vaults=(), owners=("owner",), entities=(), evidence=(),
                    supersedes=(), dedupe_key=None, data={"level": "accepted", **data}, body=body)
    return ItemRecord(record, f"fake/{number}.md")


def dated(day="2026-10-02", kind="planned", **extra):
    return {kind: {"day": day, "words": day, **extra}}


def block(item, start=NOW - timedelta(minutes=20), end=NOW + timedelta(minutes=40), **extra):
    return ScheduleBlock(work.first_id(item.record), "Old title from snapshot", start, end, None, **extra)


def reader(items, blocks=(), **sources):
    return DashboardReader(DashboardSources(items=lambda: tuple(items), calendar=lambda *_: (),
        schedule=lambda _: ScheduleSnapshot(NOW - timedelta(minutes=2), tuple(blocks), True, 120, None), **sources))


def test_canonical_next_progress_and_rank_are_used_with_parent_card_once():
    parent = thing(1, "A fake project", called="project")
    first = thing(2, "A finished step", state="done", parent=parent.record.id, order=0)
    second = thing(3, "Next fake step", parent=parent.record.id, order=1, dates=dated(),
                   timing={"duration_minutes": 30, "estimated_by": "nic"})
    result = reader([parent, first, second], [block(second)]).read(NOW)
    columns = result["work"]["columns"]
    assert [row["ref"] for row in columns[0]["items"]] == [parent.record.id]
    card = columns[0]["items"][0]
    assert card["next"] == work.next_of(parent.record, work.index_things([e.record for e in (parent, first, second)]), NOW)
    assert card["progress"]["done"] == 1 and card["progress"]["total"] == 2
    assert card["schedule_label"].startswith("Scheduled 11:00")
    assert result["day"]["now"]["title"] == "Next fake step"
    assert result["details"][second.record.id]["record_sha256"] == second.sha256


@pytest.mark.parametrize("change", [dict(state="parked"), dict(state="done"), dict(state="dropped"),
    dict(data={"level": "accepted", "needs": {"from": "Test person", "what": "A fake reply"}}),
    dict(data={"level": "accepted", "dates": dated("2026-10-04", "not_before")})])
def test_fresh_work_invalidates_old_forecast(change):
    item = thing(1, dates=dated(), timing={"duration_minutes": 30, "estimated_by": "nic"})
    stale = block(item)
    current = ItemRecord(replace(item.record, **change), item.path)
    result = reader([current], [stale]).read(NOW)
    assert result["day"]["timeline"] == []
    assert result["day"]["now"] is None


def test_deleted_task_does_not_leak_snapshot_title():
    item = thing(1)
    result = reader([], [block(item)]).read(NOW)
    assert result["day"]["timeline"] == []


@pytest.mark.parametrize("minutes,estimate", [(1440, "nic"), (960, "agent:test")])
def test_long_task_discards_stale_schedule_but_remains_visible_with_split_message(minutes, estimate):
    item = thing(1, dates=dated(), timing={"duration_minutes": minutes, "estimated_by": estimate})
    result = reader([item], [block(item)]).read(NOW)
    assert not result["day"]["timeline"] and result["day"]["now"] is None
    card = result["details"][item.record.id]
    assert card["schedule_issue"] == "needs_split"
    assert card["schedule_label"] == "Not scheduled · split this into shorter tasks"
    assert result["day"]["ranked"][0]["ref"] == item.record.id
    off = DashboardReader(DashboardSources(items=lambda: (item,))).read(NOW)
    assert off["details"][item.record.id]["schedule_issue"] is None
    assert "split this" not in str(off)


def test_container_shows_next_steps_split_message_and_oversized_dependency_invalidates_future_slot():
    parent = thing(1)
    first = thing(2, parent=parent.record.id, order=1, timing={"duration_minutes": 1440, "estimated_by": "nic"})
    second = thing(3, parent=parent.record.id, order=2, timing={"duration_minutes": 30, "estimated_by": "nic"})
    future = block(second, NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    result = reader([parent, first, second], [future]).read(NOW)
    assert not result["day"]["timeline"]
    assert result["details"][parent.record.id]["schedule_issue"] == "needs_split"


def test_parent_long_length_does_not_override_its_already_split_steps():
    parent = thing(1, timing={"duration_minutes": 1440, "estimated_by": "nic"})
    child = thing(2, parent=parent.record.id, timing={"duration_minutes": 30, "estimated_by": "nic"})
    result = reader([parent, child], [block(child)]).read(NOW)
    assert result["details"][parent.record.id]["schedule_issue"] is None
    assert result["details"][parent.record.id]["schedule_label"].startswith("Scheduled ")


@pytest.mark.parametrize("level", ["routine", "accepted"])
def test_routine_has_no_split_notice_or_stale_schedule(level):
    routine = thing(9, level=level, repeat="daily",
                    timing={"duration_minutes": 1440, "estimated_by": "nic"})
    result = reader((routine,), [block(routine)]).read(NOW)
    assert result["details"][routine.record.id]["schedule_issue"] is None
    assert not result["day"]["timeline"]


def test_fresh_title_deadline_and_kind_win_over_snapshot():
    item = thing(1, "Fresh name", dates=dated(kind="deadline", at="11:30"),
                 timing={"duration_minutes": 60, "estimated_by": "nic", "block": "deep_focus"})
    result = reader([item], [block(item)]).read(NOW)
    row = result["day"]["timeline"][0]
    assert row["title"] == "Fresh name" and row["kind"] == "deep_focus" and row["late"]
    assert "after it's due" in result["day"]["due_today"][0]["schedule_label"]


def test_needs_length_only_day_bearing_open_nonappointment_leaf():
    day = thing(1, dates=dated())
    undated = thing(2)
    parked = thing(3, state="parked", dates=dated())
    appointment = thing(4, dates=dated(kind="appointment", at="14:00"))
    sized = thing(5, dates=dated(), timing={"duration_minutes": 15, "estimated_by": "nic"})
    result = reader([day, undated, parked, appointment, sized]).read(NOW)
    assert [card["ref"] for card in result["day"]["needs_length"]] == [day.record.id]
    assert result["details"][day.record.id]["schedule_label"] == "Not scheduled yet · needs a length"


@pytest.mark.parametrize("day,group", [("2026-10-02", "due_today"), ("2026-10-03", "due_tomorrow")])
def test_due_sections_include_nested_own_deadlines_without_inherited_duplicates(day, group):
    parent = thing(1, "A parent project", called="project", dates=dated(day, "deadline"))
    inherited = thing(2, "An inherited deadline", parent=parent.record.id)
    own = thing(3, "A step with its own deadline", parent=parent.record.id, dates=dated(day, "deadline"))
    no_parent_deadline = thing(4, "Another project", called="project")
    nested = thing(5, "A nested deadline", parent=no_parent_deadline.record.id, dates=dated(day, "deadline"))
    gone = thing(6, "A finished deadline", state="done", dates=dated(day, "deadline"))
    parked = thing(7, "A parked deadline", state="parked", dates=dated(day, "deadline"))
    result = reader([parent, inherited, own, no_parent_deadline, nested, gone, parked]).read(NOW)
    assert {row["ref"] for row in result["day"][group]} == {parent.record.id, own.record.id, nested.record.id}
    assert result["details"][inherited.record.id]["dates"]["deadline"]["day"] == day


def test_nested_legacy_deadline_is_read_from_canonical_mapping():
    parent = thing(1, called="project")
    child = thing(2, parent=parent.record.id, timing={"due": "2026-10-02"})
    result = reader([parent, child]).read(NOW)
    assert [row["ref"] for row in result["day"]["due_today"]] == [child.record.id]


def test_unavailable_snapshot_falls_back_to_rank_without_stale_slots():
    item = thing(1)
    read = DashboardReader(DashboardSources(items=lambda: (item,), calendar=lambda *_: (),
        schedule=lambda _: ScheduleSnapshot(NOW, (block(item),), False, 800)))
    result = read.read(NOW)["day"]
    assert result["schedule"]["label"] == "Couldn't see the schedule"
    assert result["timeline"] == [] and result["ranked"][0]["ref"] == item.record.id


def test_calendar_failure_visible_and_exception_text_never_exposed():
    def fail(*_):
        raise RuntimeError("secret provider text")
    result = DashboardReader(DashboardSources(items=lambda: (), calendar=fail, schedule=fail)).read(NOW)["day"]
    assert result["notices"] == ["Couldn't see the schedule", "Couldn't see your calendar"]
    assert "secret" not in str(result)
    with pytest.raises(DashboardUnavailable, match="Couldn't see your work"):
        DashboardReader(DashboardSources(items=fail)).read(NOW)


def test_superseded_record_resolves_to_first_id_fresh_details():
    old = thing(1, "Old", timing={"duration_minutes": 30, "estimated_by": "nic"})
    new = ItemRecord(replace(old.record, id=thing(2).record.id, title="New", supersedes=(old.record.id,),
                             updated_at=NOW), "fake/current.md")
    result = reader([old, new], [block(old)]).read(NOW)
    assert list(result["details"]) == [old.record.id]
    assert result["details"][old.record.id]["path"] == "fake/current.md"
    assert result["day"]["now"]["title"] == "New"


def test_removing_length_removes_old_schedule_and_returns_to_needs_length():
    item = thing(1, dates=dated())
    result = reader([item], [block(item)]).read(NOW)
    assert not result["day"]["timeline"]
    assert result["day"]["needs_length"][0]["ref"] == item.record.id


def test_quick_add_question_mark_stays_for_review():
    item = thing(1, review_marks=[{"mark": "question", "text": "Review this new thing"}])
    result = reader([item]).read(NOW)
    assert result["work"]["columns"][4]["items"][0]["ref"] == item.record.id


def test_done_count_uses_recorded_done_day_not_latest_edit():
    item = thing(1, state="done", dates=dated("2026-10-01", "done"))
    assert reader([item]).read(NOW)["day"]["done_count"] == 0


def test_unsized_container_remains_in_needs_length():
    parent = thing(1, called="project", dates=dated())
    child = thing(2, parent=parent.record.id)
    data = reader([parent, child]).read(NOW)["day"]
    assert data["needs_length"][0]["ref"] == parent.record.id


def test_future_dependent_forecast_and_parent_wait_follow_schedule_reader():
    parent = thing(1, called="project")
    first = thing(2, parent=parent.record.id, timing={"duration_minutes": 30, "estimated_by": "nic"})
    second = thing(3, parent=parent.record.id, after=[first.record.id],
                   timing={"duration_minutes": 30, "estimated_by": "nic"})
    future = block(second, NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    assert reader([parent, first, second], [future]).read(NOW)["day"]["next"]["ref"] == second.record.id
    waiting = ItemRecord(replace(parent.record, data={**parent.record.data,
                         "needs": {"from": "Test person", "what": "A reply"}}), parent.path)
    data = reader([waiting, first, second], [future]).read(NOW)["day"]
    assert not data["timeline"] and not data["ranked"]


def test_singapore_personal_day_and_calendar_bounds_before_four():
    calls = []
    def calendar(start, end):
        calls.append((start, end))
        return (CalendarEvent("Fake midnight event", start + timedelta(hours=21), start + timedelta(hours=22)),)
    read = DashboardReader(DashboardSources(items=lambda: (), calendar=calendar))
    data = read.read(datetime(2026, 10, 3, 1, 30, tzinfo=SGT))["day"]
    assert data["date"] == "2026-10-02"
    assert calls == [(datetime(2026, 10, 2, 4, tzinfo=SGT), datetime(2026, 10, 3, 4, tzinfo=SGT))]
    assert data["now"]["title"] == "Fake midnight event"


def app_for(read, *, enabled=True, reads=True):
    app = FastAPI()
    app.state.dashboard_settings = SimpleNamespace(enabled=enabled, reads=reads, writes=False, ask_diablo=False)
    def owner(request: Request):
        if request.headers.get("test-owner") != "yes":
            raise HTTPException(401, "Sign in")
        return SimpleNamespace(owner_id="fake-owner")
    app.include_router(create_read_router(read, owner_dependency=owner, clock=lambda: NOW))
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/day", "/api/work", "/api/work/fake", "/app", "/assets/dashboard.js", "/assets/dashboard.css"])
def test_every_route_needs_owner_before_any_source_is_read(path):
    calls = []
    read = DashboardReader(DashboardSources(items=lambda: calls.append(1) or ()))
    response = app_for(read).get(path)
    assert response.status_code == 401 and not calls


@pytest.mark.parametrize("enabled,reads", [(False, True), (True, False), (False, False)])
def test_switches_refuse_without_reads(enabled, reads):
    calls = []
    read = DashboardReader(DashboardSources(items=lambda: calls.append(1) or ()))
    response = app_for(read, enabled=enabled, reads=reads).get("/api/day", headers={"test-owner": "yes"})
    assert response.status_code == 404 and not calls


def test_each_redraw_reads_source_fresh_and_never_cache_response():
    items = [thing(1, "First name")]
    client = app_for(reader(items))
    first = client.get("/api/work", headers={"test-owner": "yes"})
    items[0] = ItemRecord(replace(items[0].record, title="Changed elsewhere"), items[0].path)
    second = client.get("/api/work/" + items[0].record.id, headers={"test-owner": "yes"})
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
    assert second.json()["title"] == "Changed elsewhere"
    assert second.json()["capabilities"] == {"writes": False, "ask_diablo": False}


def test_page_is_empty_readonly_shell_and_assets_do_not_execute_record_html():
    client = app_for(reader([thing(1, "<img onerror=alert(1)>")]))
    headers = {"test-owner": "yes"}
    page = client.get("/app", headers=headers).text
    script = client.get("/assets/dashboard.js", headers=headers).text
    assert "<img" not in page and "actions.js" not in page
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "textContent" in script and "draggable" not in script
    assert "https://" not in page and "onclick=" not in page and "<style" not in page
    assert client.get("/assets/reads.py", headers=headers).status_code == 404
