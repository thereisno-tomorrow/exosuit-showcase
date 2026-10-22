"""A card's local calendar window must not change the existing dashboard default."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from gremlin_town.dashboard.reads import DashboardReader, DashboardSources


def test_review_uses_local_midnight_window_and_keeps_overnight_commitment():
    zone = ZoneInfo("Australia/Sydney")
    now = datetime(2026, 10, 4, 8, 35, tzinfo=zone)
    event = SimpleNamespace(title="Overnight shift", start=datetime(2026,10,3,20,tzinfo=zone),
        end=datetime(2026,10,4,10,tzinfo=zone), all_day=False, exosuit_role=None)
    calls = []
    def calendar(start,end):
        calls.append((start,end))
        return (event,)
    reader = DashboardReader(DashboardSources(lambda: (), calendar=calendar))
    view = reader.read(now, day=now.date(), zone=zone, calendar_day=True)
    start,end = calls[0]
    assert start == datetime(2026,10,4,0,tzinfo=zone)
    assert end == datetime(2026,10,5,0,tzinfo=zone)
    assert (end.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds() == 23*3600
    assert view["day"]["date"] == "2026-10-04"
    assert view["day"]["timeline"][0]["start"] == event.start.isoformat()
    assert view["day"]["timeline"][0]["end"] == event.end.isoformat()


def test_legacy_dashboard_keeps_existing_four_am_singapore_window():
    zone = timezone(timedelta(hours=8))
    now = datetime(2026,10,4,8,tzinfo=zone)
    reader = DashboardReader(DashboardSources(lambda: (),calendar=lambda *_: ()))
    view = reader.read(now,day=now.date())
    assert datetime.fromisoformat(view["day"]["timeline_start"]).hour == 4


def test_new_explicit_pick_day_is_used_before_singapore_morning(monkeypatch):
    import gremlin_town.dashboard.reads as reads
    now=datetime(2026,10,4,0,30,tzinfo=ZoneInfo("Australia/Sydney"))
    row=SimpleNamespace(item_ref="example",response="picked_today",responded_at=now,
                        surfaced_at=now,fields_used={"review_day":"2026-10-04"})
    seen=[]
    original=reads.project_work
    def capture(entries,clock,**kwargs):
        seen.append(kwargs["picked_today"])
        return original(entries,clock,**kwargs)
    monkeypatch.setattr(reads,"project_work",capture)
    reader=DashboardReader(DashboardSources(lambda: (),calendar=lambda *_: (),history=lambda: (row,)))
    reader.read(now,day=now.date(),zone=now.tzinfo,calendar_day=True)
    assert seen == [frozenset({"example"})]
