"""The placement runner (step I; build plan slice 4): `gremlin_town.review.placement`, with fake
sources and a fake calendar writer. The day is Thursday 1 Oct 2026, Singapore time."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Evidence, Record
from brain_core.review import CalendarEvent, HistoryRow, ItemRecord
from brain_core.timewords import SINGAPORE

from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.placement import (
    DayChangeError,
    PlacementContext,
    clear_future,
    parse_day_change,
    run_placement,
    status,
)
from gremlin_town.review.settings import PlacementSettings, PlacementSettingsError
from gremlin_town.review.why_log import WhyLog
from gremlin_town.sources.google.calendar_writer import CalendarWriteError, EventSlot, PlacedEntry
from gremlin_town.sources.result import UnavailableReason

DAY = date(2026, 10, 1)
EVIDENCE = (Evidence(source_type="session_message", locator="session:test:1", content_sha256="a" * 64, excerpt="fixture"),)


def at(clock: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=SINGAPORE)


def item(suffix: str, *, block: str = "admin", minutes: int | None = 30, due: str | None = None) -> ItemRecord:
    timing: dict[str, object] = {}
    if minutes is not None:
        timing.update(duration_minutes=minutes, padded_minutes=minutes, estimated_by="nic")
    if due is not None:
        timing["due"] = due
    created = datetime(2026, 9, 30, 12, 0, tzinfo=SINGAPORE)
    return ItemRecord(Record(
        schema=RECORD_SCHEMA_V2, id=f"brn_20260930_120000_{suffix}", kind="item", title=suffix, state="open",
        created_at=created, updated_at=created, assertion=None, confidence=None, sensitivity="private", vaults=(),
        owners=(), entities=(), evidence=EVIDENCE, supersedes=(), dedupe_key=None,
        data={"level": "accepted", "size": "task", "timing": timing, "block": block}, body="",
        source={"said_by": "owner", "came_by": "direct", "words": "fixture", "recorded_by": "chatgpt-web"}, links=(),
    ), f"items/{suffix}.md")


def pick(record: ItemRecord, when: datetime) -> HistoryRow:
    return HistoryRow(
        item_ref=record.record.id, channel="review", rule="r", record_sha256="x", record_state="open",
        surfaced_at=when, response="picked_today", responded_at=when,
    )


class FakeWriter:
    """The placed-entry half of CalendarWriter, with its guards: future only, no guests."""

    def __init__(self) -> None:
        self.entries: dict[str, PlacedEntry] = {}
        self.calls: list[str] = []
        self.fail: bool = False
        self._next = 0

    def _check(self, entry: PlacedEntry, now: datetime, new_slot=None) -> None:
        if entry.has_guests or entry.slot is None:
            raise ValueError("guests")
        if entry.slot.start > now:
            return
        if new_slot is not None and entry.slot.end > now and new_slot.start == entry.slot.start and new_slot.end > now:
            return
        raise ValueError("only an entry that starts in the future is moved or removed")

    def list_placed(self, day):
        self.calls.append("list")
        return tuple(entry for entry in self.entries.values() if entry.day == day)

    def insert_placed(self, *, day, role, key, title, description, slot, busy, now):
        if self.fail:
            raise CalendarWriteError(UnavailableReason.RATE_LIMITED, "slow down")
        if slot.start <= now:
            raise ValueError("future")
        self._next += 1
        entry = PlacedEntry(f"evt{self._next}", day, role, key, title, slot, False, busy)
        self.entries[entry.event_id] = entry
        self.calls.append(f"insert {role} {key}")
        return entry

    def move_placed(self, entry, slot, *, now, title=None):
        self._check(entry, now, slot)
        moved = replace(entry, slot=slot, title=title or entry.title)
        self.entries[entry.event_id] = moved
        self.calls.append(f"move {entry.role} {entry.key}")
        return moved

    def delete_placed(self, entry, *, now):
        self._check(entry, now)
        self.entries.pop(entry.event_id, None)
        self.calls.append(f"delete {entry.role} {entry.key}")

    def by_task(self) -> dict[str, list[tuple[str, str]]]:
        found: dict[str, list[tuple[str, str]]] = {}
        for entry in self.entries.values():
            name = entry.key.split(":")[0] if entry.role == "slot" else f"block {entry.key}"
            found.setdefault(name, []).append((entry.slot.start.strftime("%H:%M"), entry.slot.end.strftime("%H:%M")))
        return {name: sorted(times) for name, times in found.items()}

    def times(self) -> dict[str, tuple[str, str]]:
        return {
            f"{entry.role} {entry.key}": (entry.slot.start.strftime("%H:%M"), entry.slot.end.strftime("%H:%M"))
            for entry in self.entries.values()
        }


class Day:
    """One day's fakes: Brain Home items, the calendar, the why-log, the writer."""

    def __init__(self, *items: ItemRecord, write: bool = True, events: tuple[CalendarEvent, ...] = (), **settings) -> None:
        self.items = list(items)
        self.events = list(events)
        self.history: list[HistoryRow] = []
        self.why_log = WhyLog(":memory:")
        self.writer = FakeWriter()
        self.calendar_fails = False
        self.context = PlacementContext(
            settings=PlacementSettings(enabled=True, write=write, **settings),
            why_log=self.why_log,
            sources=ReviewSources(
                brain_items=lambda: tuple(self.items), calendar_events=self._events, brain_preferences=lambda: (),
            ),
            writer=self.writer,
            history=lambda: tuple(self.history),
        )

    def _events(self, start, end):
        if self.calendar_fails:
            raise RuntimeError("calendar down")
        return tuple(self.events)

    def pick(self, record: ItemRecord, when: datetime) -> None:
        self.history.append(pick(record, when))

    def settle(self, when: datetime) -> None:
        self.why_log.record_signal(kind="day_settled", actor="hermes-wa", at=when)

    def change(self, when: datetime, **detail) -> None:
        self.why_log.record_signal(kind="day_change", actor="hermes-wa", at=when, detail=json.dumps(detail))

    def run(self, when: datetime, **kwargs):
        return run_placement(self.context, when, **kwargs)


def meeting(start: str, end: str, **changes) -> CalendarEvent:
    return CalendarEvent(title="Meeting", start=at(start), end=at(end), **changes)


# --- when it runs --------------------------------------------------------------------------------


def test_off_reads_nothing():
    day = Day(item("bank"))
    day.context.settings = PlacementSettings()
    assert day.run(at("08:00")) == {"outcome": "disabled"}
    assert day.writer.calls == []


def test_nothing_is_drawn_before_the_day_is_settled():
    bank = item("bank")
    day = Day(bank)
    assert day.run(at("06:30"))["outcome"] == "not_settled"
    day.pick(bank, at("06:30"))
    assert day.run(at("06:40"))["outcome"] == "not_settled"  # 10 minutes after the last pick
    assert day.writer.calls == []


def test_day_settled_draws_the_day_and_writes_it():
    bank, essay = item("bank"), item("essay", block="deep_focus", minutes=120)
    day = Day(bank, essay)
    day.pick(bank, at("06:30"))
    day.pick(essay, at("06:31"))
    day.settle(at("06:32"))
    result = day.run(at("06:33"))
    assert result["outcome"] == "written"
    assert result["summary"][0].startswith("Today: admin 9 am–9:30 am (1 task) · deep focus 9:30 am–11:30 am")
    assert day.writer.times() == {
        "block admin:0900": ("09:00", "09:30"),
        "block deep_focus:0930": ("09:30", "11:30"),
        f"slot {bank.record.id}:1@0900": ("09:00", "09:30"),
        f"slot {essay.record.id}:1@0930": ("09:30", "11:30"),
    }
    assert sorted(result["calendar"]["added"])[0].startswith("block admin:0900")


def test_picks_alone_never_settle_the_day():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    for clock in ("06:50", "08:00", "17:00"):
        assert day.run(at(clock))["outcome"] == "not_settled"
    assert day.writer.calls == []
    assert day.why_log.latest_placement(DAY) is None


def test_plan_only_writes_nothing_and_still_says_the_plan():
    bank = item("bank")
    day = Day(bank, write=False)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    result = day.run(at("06:32"))
    assert result["outcome"] == "planned" and result["summary"][0].startswith("Today: admin")
    assert day.writer.calls == [] and "calendar" not in result


def test_a_day_off_draws_nothing():
    saturday = date(2026, 10, 3)
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30", saturday))
    day.settle(at("06:31", saturday))
    assert day.run(at("06:32", saturday))["outcome"] == "day_off"
    assert day.writer.calls == []


def test_the_calendar_unreadable_means_nothing_planned_or_written():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.calendar_fails = True
    result = day.run(at("06:32"))
    assert result["outcome"] == "calendar_unavailable" and "calendar down" in result["error"]
    assert day.writer.calls == [] and day.why_log.latest_placement() is None


# --- quiet when nothing changed ------------------------------------------------------------------


def test_an_unchanged_day_reads_and_writes_nothing_more():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    day.writer.calls.clear()
    assert day.run(at("06:35"))["outcome"] == "unchanged"
    assert day.run(at("07:10"))["outcome"] == "unchanged"
    assert day.writer.calls == []


def test_a_new_meeting_moves_only_the_slot_it_lands_on():
    bank, forms = item("bank"), item("forms")
    day = Day(bank, forms)
    day.pick(bank, at("06:30"))
    day.pick(forms, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    before = day.writer.by_task()
    moved = next(name for name, times in before.items() if times == [("09:30", "10:00")])
    day.events.append(meeting("09:30", "09:50"))
    result = day.run(at("07:00"))
    after = day.writer.by_task()
    # The slot the meeting landed on moves after it, and its admin block grows to hold it.
    assert after[moved] == [("10:00", "10:30")]
    assert after["block admin:0900"] == [("09:00", "10:30")]
    assert {name for name in before if before[name] != after[name]} == {moved, "block admin:0900"}
    assert result["outcome"] == "written"


# --- one-line changes and moves -------------------------------------------------------------------


def test_no_deep_focus_today_redraws_from_now():
    bank, essay = item("bank"), item("essay", block="deep_focus", minutes=120)
    day = Day(bank, essay)
    for record in (bank, essay):
        day.pick(record, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    assert "block deep_focus:0930" in day.writer.times()
    day.change(at("07:00"), deep_focus_minutes=0, words="no deep focus today")
    result = day.run(at("07:01"))
    assert "block deep_focus:0930" not in day.writer.times()
    assert any("Doesn't fit today" in line for line in result["summary"])


def test_done_at_four_redraws_and_past_entries_stay():
    records = [item(f"a{n}", minutes=60) for n in range(9)]
    day = Day(*records)
    for record in records:
        day.pick(record, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    past = {key: value for key, value in day.writer.times().items() if key.startswith("slot") and value[0] < "10:05"}
    day.change(at("10:05"), ends_at="16:00", words="I'm done at 4")
    result = day.run(at("10:06"))
    after = day.writer.times()
    assert all(after.get(key) == value for key, value in past.items())
    # The block under way keeps its start and now ends at 4; nothing runs past 4.
    assert after["block admin:0900"] == ("09:00", "16:00")
    assert max(end for start, end in after.values()) <= "16:00"
    assert any("Doesn't fit today" in line for line in result["summary"])


def test_move_hands_a_passed_slot_back():
    a, b = item("a"), item("b")
    day = Day(a, b)
    day.pick(a, at("06:30"))
    day.pick(b, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    day.run(at("09:35"))  # a's 9:00-9:30 slot passed, a still open: asked about, not moved
    assert day.writer.by_task()[a.record.id] == [("09:00", "09:30")]
    day.change(at("09:40"), move=a.record.id, words="move it")
    day.run(at("09:41"))
    a_slots = [value for key, value in day.writer.times().items() if key.startswith(f"slot {a.record.id}")]
    assert ("09:00", "09:30") in a_slots and any(start >= "10:00" for start, _ in a_slots)


# --- failures and guards -------------------------------------------------------------------------


def test_a_write_failure_keeps_the_plan_and_the_next_run_tries_again():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.writer.fail = True
    failed = day.run(at("06:32"))
    assert failed["outcome"] == "write_failed" and "slow down" in failed["error"]
    assert day.why_log.latest_placement(DAY)["outcome"] == "write_failed"
    day.writer.fail = False
    assert day.run(at("06:35"))["outcome"] == "written"
    assert f"slot {bank.record.id}:1@0900" in day.writer.times()


def test_an_entry_the_writer_refuses_is_left_alone():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    # A block moved by hand is put back, unless someone has given it guests: then it's left alone.
    event_id, entry = next((k, v) for k, v in day.writer.entries.items() if v.role == "block")
    day.writer.entries[event_id] = replace(entry, has_guests=True, slot=EventSlot(at("12:00"), at("12:30")))
    day.events.append(meeting("15:00", "16:00"))  # something the runner watches changes, so it runs again
    result = day.run(at("06:40"))
    assert result["calendar"]["left_alone"] == ["block admin:0900 09:00-09:30"]
    assert day.writer.entries[event_id].has_guests


def test_dry_run_reads_and_reports_but_writes_and_records_nothing():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    result = day.run(at("06:32"), dry_run=True)
    assert result["calendar"]["added"] and day.writer.entries == {}
    assert day.why_log.latest_placement() is None


def test_status_and_clear_future():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    shown = status(day.why_log)
    assert shown["outcome"] == "written" and shown["summary"][0].startswith("Today: admin")
    assert clear_future(day.writer, at("09:10")) == {"removed": [], "skipped": []} and day.writer.entries  # started: left
    event_id, entry = next((k, v) for k, v in day.writer.entries.items() if v.role == "block")
    day.writer.entries[event_id] = replace(entry, has_guests=True)
    cleared = clear_future(day.writer, at("08:00"))
    assert cleared == {"removed": [f"slot {bank.record.id}:1@0900"], "skipped": ["block admin:0900"]}


# --- inputs ---------------------------------------------------------------------------------------


def test_free_all_day_and_own_entries_are_not_busy():
    bank = item("bank")
    day = Day(bank, events=(
        meeting("09:00", "10:00", busy=False),
        CalendarEvent(title="Holiday", start=at("00:00"), end=at("00:00") + timedelta(days=1), all_day=True),
        meeting("09:00", "09:30", exosuit_role="slot"),
    ))
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    assert day.writer.times()[f"slot {bank.record.id}:1@0900"] == ("09:00", "09:30")


@pytest.mark.parametrize("detail", [
    None, "not json", "[]", "{}", '{"words": "only words"}', '{"deep_focus_minutes": 5}', '{"deep_focus_minutes": true}',
    '{"deep_focus_minutes": 60}', '{"deep_focus_minutes": 400}',
    '{"ends_at": "4pm"}', '{"ends_at": 1600}', '{"ends_at": "16"}', '{"ends_at": "T16:00"}', '{"ends_at": "16:00:30"}',
    '{"ends_at": "16:00+08:00"}', '{"ends_at": "24:00"}', '{"move": ""}', '{"lunch": 30}',
])
def test_day_change_details_are_checked(detail):
    with pytest.raises(DayChangeError):
        parse_day_change(detail)


def test_day_change_details_that_pass():
    assert parse_day_change('{"deep_focus_minutes": 0, "words": "no deep focus today"}')["deep_focus_minutes"] == 0
    assert parse_day_change('{"ends_at": "16:00"}')["ends_at"] == "16:00"
    assert parse_day_change('{"deep_focus_minutes": 90}') and parse_day_change('{"deep_focus_minutes": 360}')
    assert parse_day_change('{"move": "brn_x"}')["move"] == "brn_x"


def test_settings_are_checked():
    assert PlacementSettings.from_table(None, review_enabled=False) == PlacementSettings()
    assert PlacementSettings.from_table({"enabled": True, "write": True}, review_enabled=True).write
    for table, review in (
        ({"enabled": True}, False),
        ({"lunch": 1}, True),
        ({"write": "yes"}, True),
        ({"deep_focus_max_minutes": 700}, True),
        ({"deep_focus_min_minutes": 300, "deep_focus_max_minutes": 200}, True),
        ({"settle_after_minutes": 0}, True),
    ):
        with pytest.raises(PlacementSettingsError):
            PlacementSettings.from_table(table, review_enabled=review)



# --- slice 4 review fixes -------------------------------------------------------------------------


def test_a_bad_stored_change_is_skipped_and_reported_never_stopping_the_day():
    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    day.why_log.record_signal(kind="day_change", actor="hermes-wa", at=at("06:31"), detail='{"ends_at": 1600}')
    result = day.run(at("06:32"))
    assert result["outcome"] == "written" and len(result["skipped_changes"]) == 1


def test_switching_write_on_writes_at_the_next_run():
    bank = item("bank")
    day = Day(bank, write=False)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    assert day.run(at("06:32"))["outcome"] == "planned" and day.writer.entries == {}
    day.context.settings = PlacementSettings(enabled=True, write=True)
    assert day.run(at("06:35"))["outcome"] == "written" and day.writer.entries


def test_a_partial_calendar_read_counts_as_none():
    from gremlin_town.review.collect import SourceUnavailable

    bank = item("bank")
    day = Day(bank)
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))

    def partial(start, end):
        raise SourceUnavailable("partial")

    day.context.calendar_events = partial
    assert day.run(at("06:32"))["outcome"] == "calendar_unavailable" and day.writer.calls == []


def test_one_run_at_a_time_and_a_stale_lock_is_taken_over(tmp_path):
    bank = item("bank")
    day = Day(bank)
    day.why_log = WhyLog(tmp_path / "why.sqlite3")
    day.context.why_log = day.why_log
    day.pick(bank, at("06:30"))
    day.settle(at("06:31"))
    other = WhyLog(tmp_path / "why.sqlite3")  # the other process: the hub's signal route
    assert other.take_placement_lock("route", at("06:32"), stale_after=timedelta(minutes=5))
    assert day.run(at("06:33")) == {"outcome": "busy"} and day.writer.calls == []
    assert day.run(at("06:40"))["outcome"] == "written"  # the route's lock went stale after 5 minutes
    other.release_placement_lock("route")  # a stale holder's release no longer frees anything
    assert other.take_placement_lock("route", at("06:41"), stale_after=timedelta(minutes=5))
    other.release_placement_lock("route")


def test_the_why_log_keeps_no_task_text_but_the_reply_still_names_tasks():
    bank = item("bank")
    huge = item("huge", minutes=600)
    day = Day(bank, huge)
    day.pick(bank, at("06:30"))
    day.pick(huge, at("06:30"))
    day.settle(at("06:31"))
    result = day.run(at("06:32"))
    stored = json.dumps(day.why_log.latest_placement(DAY)["plan"])
    assert "bank" in stored  # record IDs, which carry the fixture's suffix
    assert '"task_title": ""' in stored and '"title": ""' in stored
    assert any('"huge"' in line for line in result["summary"])
    again = day.run(at("06:35"))
    assert again["outcome"] == "unchanged" and any('"huge"' in line for line in again["summary"])



# --- slice 7: slots moved by hand, and the daily repair ------------------------------------------


def _settled_day(*records, **settings):
    day = Day(*records, **settings)
    for record in records:
        day.pick(record, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    return day


def _entry(day, prefix):
    return next((k, v) for k, v in day.writer.entries.items() if f"{v.role} {v.key}".startswith(prefix))


def test_a_slot_nic_moves_by_hand_stays_where_he_put_it_and_the_rest_fits_around():
    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    assert day.writer.by_task()[b.record.id] == [("09:30", "10:00")]
    event_id, entry = _entry(day, f"slot {a.record.id}")
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(at("09:30"), at("10:00")))  # dragged onto b's time
    day.events.append(meeting("15:00", "16:00"))  # a change, so the runner goes again
    result = day.run(at("07:00"))
    assert result["kept_as_moved"] == [f"{a.record.id}:1"]
    after = day.writer.by_task()
    assert after[a.record.id] == [("09:30", "10:00")]  # his time wins
    assert after[b.record.id] != [("09:30", "10:00")]  # b made room
    assert day.writer.entries[event_id].key == entry.key  # the same entry he moved, not a new one
    day.events.append(meeting("16:00", "16:30"))
    day.run(at("07:10"))
    assert day.writer.by_task()[a.record.id] == [("09:30", "10:00")]  # and it stays there


def test_the_daily_repair_puts_back_what_was_deleted_by_hand_once_a_day():
    a = item("a")
    day = _settled_day(a)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    del day.writer.entries[event_id]  # deleted by hand
    assert day.run(at("09:00"))["outcome"] == "unchanged"  # before the repair hour: nothing read
    repaired = day.run(at("08:00") + timedelta(hours=4))  # 12:00: a's 9:00 slot is past, so it stays gone
    assert repaired["repaired"] is True
    assert not any(v.role == "slot" for v in day.writer.entries.values())
    assert repaired["calendar"]["added"] == []  # nothing past is ever put back


def test_the_repair_puts_back_future_entries_only_and_runs_once_a_day():
    a, b = item("a"), item("b", minutes=60)
    day = Day(a, b, events=(meeting("09:00", "12:30"),))  # the day's work starts after 12:40
    for record in (a, b):
        day.pick(record, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    planned = day.writer.times()
    assert planned[f"slot {a.record.id}:1@1240"] == ("12:40", "13:10")
    for event_id in [k for k, v in day.writer.entries.items() if v.role == "slot"]:
        del day.writer.entries[event_id]  # both slots deleted by hand
    block_id, block = _entry(day, "block")
    day.writer.entries[block_id] = replace(block, slot=EventSlot(at("13:00"), block.slot.end))  # block dragged
    result = day.run(at("12:00"))
    assert result["repaired"] is True
    assert day.writer.times() == planned  # every entry still to come is back where the plan has it
    day.writer.calls.clear()
    assert day.run(at("12:30"))["outcome"] == "unchanged" and day.writer.calls == []  # once a day


def test_no_repair_while_plan_only():
    a = item("a")
    day = _settled_day(a, write=False)
    result = day.run(at("12:00"))
    assert "repaired" not in result and day.writer.calls == []



# --- placement-02: pins last the day; a slot dragged to another day is his; failed writes ---------


def test_a_resized_slot_outside_its_block_stays_pinned_through_later_changes():
    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(at("15:00"), at("15:10")))  # dragged out and shortened
    day.events.append(meeting("16:00", "16:30"))
    first = day.run(at("07:00"))
    assert first["kept_as_moved"] == [f"{a.record.id}:1"]
    for n, clock in enumerate(("07:10", "07:20", "07:30")):
        day.events.append(meeting(f"17:{n}0", f"17:{n}5"))  # the day keeps changing
        day.run(at(clock))
        assert day.writer.by_task()[a.record.id] == [("15:00", "15:10")]


def test_a_slot_dragged_to_another_day_becomes_his_and_leaves_today():
    from datetime import date as _date

    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    tomorrow = at("10:00", _date(2026, 10, 2))
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(tomorrow, tomorrow + timedelta(minutes=30)))
    day.events.append(meeting("16:00", "16:30"))
    result = day.run(at("07:00"))
    assert day.writer.entries[event_id].slot.start == tomorrow  # untouched where he put it
    assert not [call for call in day.writer.calls if call.startswith(("delete slot " + a.record.id, "move slot " + a.record.id))]
    assert a.record.id not in [slot["key"] for slot in result["plan"]["slots"]]  # gone from today
    assert f"slot {a.record.id}:1@0900" in {f"{v.role} {v.key}" for v in day.writer.entries.values()}  # the moved one only
    day.run(at("12:00"))  # the repair doesn't bring it back either
    assert [v for v in day.writer.entries.values() if v.role == "slot" and v.key.startswith(a.record.id)] == [day.writer.entries[event_id]]


def test_a_failed_write_is_never_read_back_as_nics_move():
    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    day.events.append(meeting("09:00", "09:25"))  # the block and both slots must move
    real_move = day.writer.move_placed
    moves = []

    def one_move_then_fail(entry, slot, *, now, title=None):
        moves.append(entry.key)
        if len(moves) > 1:
            raise CalendarWriteError(UnavailableReason.RATE_LIMITED, "slow down")
        return real_move(entry, slot, now=now, title=title)

    day.writer.move_placed = one_move_then_fail  # the first write lands, the next fails
    failed = day.run(at("07:00"))
    assert failed["outcome"] == "write_failed" and len(moves) == 2
    day.writer.move_placed = real_move
    retried = day.run(at("07:05"))
    assert retried["outcome"] == "written" and "kept_as_moved" not in retried  # the landed move isn't taken for his


def test_the_why_log_is_safe_to_share_between_threads(tmp_path):
    import threading

    log = WhyLog(tmp_path / "why.sqlite3")
    errors = []

    def work(n):
        try:
            for i in range(50):
                log.record_signal(kind="pc_on", actor=f"t{n}", at=at("06:00") + timedelta(seconds=i))
                log.signals_on("pc_on", DAY)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(log.signals_on("pc_on", DAY)) == 200



# --- placement-02 review: pins of finished tasks, dragging back, the all-day row, split parts -----


def test_a_pin_on_a_task_since_done_is_let_go_and_his_event_never_renamed():
    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(at("15:00"), at("15:30")))
    day.events.append(meeting("16:00", "16:30"))
    day.run(at("07:00"))  # pinned at 15:00
    day.items = [replace(a, record=replace(a.record, state="done")), b]  # then he marks it done
    day.change(at("07:05"), ends_at="17:00", words="done at 5")  # and changes his day: a redraw
    day.run(at("07:06"))
    assert all(v.title != "\u25b8 " and v.title for v in day.writer.entries.values())
    assert f"slot {a.record.id}" not in {f"{v.role} {v.key.split(':')[0]}" for v in day.writer.entries.values()}


def test_a_redraw_keeps_a_pinned_part_and_places_the_rest_of_the_task():
    essay = item("essay", block="deep_focus", minutes=180)
    day = Day(essay, events=(meeting("11:00", "12:20"),))  # free 9-11 and 12:30 on: two parts
    day.pick(essay, at("06:30"))
    day.settle(at("06:31"))
    day.run(at("06:32"))
    parts = sorted(v.slot.start for v in day.writer.entries.values() if v.role == "slot")
    assert len(parts) == 2
    event_id, entry = next((k, v) for k, v in day.writer.entries.items() if v.role == "slot" and v.slot.start == parts[0])
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(at("13:00"), at("15:00")))  # part 1 dragged
    day.events.append(meeting("17:00", "17:30"))
    day.run(at("07:00"))
    day.change(at("07:05"), ends_at="17:30", words="done at half five")
    result = day.run(at("07:06"))
    slots = [slot for slot in result["plan"]["slots"] if slot["key"] == essay.record.id]
    minutes = sum((datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])) // timedelta(minutes=1) for s in slots)
    assert minutes == 180 and result["plan"]["unplaced"] == []  # the pinned 2 hours kept, the last hour placed
    assert sorted(slot["part"] for slot in slots) == [1, 2]  # no two slots share a part
    pinned = next(slot for slot in slots if slot["part"] == 1)
    assert (pinned["start"][11:16], pinned["end"][11:16]) == ("13:00", "15:00")


def test_a_slot_dragged_to_tomorrow_and_back_is_todays_again():
    from datetime import date as _date

    a = item("a")
    day = _settled_day(a)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    tomorrow = at("10:00", _date(2026, 10, 2))
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(tomorrow, tomorrow + timedelta(minutes=30)))
    day.events.append(meeting("16:00", "16:30"))
    day.run(at("07:00"))
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(at("14:00"), at("14:30")))  # dragged back to today
    day.events.append(meeting("16:30", "17:00"))
    result = day.run(at("07:10"))
    assert event_id in day.writer.entries and day.writer.entries[event_id].slot.start == at("14:00")
    assert a.record.id in [slot["key"] for slot in result["plan"]["slots"]]


def test_a_slot_dragged_into_the_all_day_row_is_his_and_never_breaks_a_run():
    a, b = item("a"), item("b")
    day = _settled_day(a, b)
    event_id, entry = _entry(day, f"slot {a.record.id}")
    day.writer.entries[event_id] = replace(entry, slot=EventSlot(DAY, DAY + timedelta(days=1)))
    day.events.append(meeting("16:00", "16:30"))
    result = day.run(at("07:00"))
    assert result["outcome"] == "written"
    assert day.writer.entries[event_id].slot.all_day  # untouched
    assert a.record.id not in [slot["key"] for slot in result["plan"]["slots"]]
    assert day.run(at("12:00"))["outcome"] in ("written", "unchanged")  # the repair too


# --- where the time went (30 Sep 2026: settling the day took 19.2 s of Diablo's 20 s) -----------


def test_a_run_that_reads_says_where_its_time_went():
    bank = item("bank")
    day = Day(bank)
    assert "seconds" not in day.run(at("06:30"))  # not settled: nothing read, no timing
    day.pick(bank, at("06:30"))
    day.settle(at("06:32"))
    result = day.run(at("06:33"))
    assert result["outcome"] == "written"
    seconds = result["seconds"]
    assert set(seconds) == {"brain_home", "calendar_read", "calendar_write", "total", "item_files"}
    assert seconds["item_files"] == 1
    assert all(isinstance(seconds[name], float) and seconds[name] >= 0 for name in seconds if name != "item_files")
    assert seconds["total"] >= max(seconds["brain_home"], seconds["calendar_read"], seconds["calendar_write"])
    assert "bank" not in json.dumps(seconds)  # parts and counts only, never a title
    # The stored plan is unchanged by it: the next run finds the day unchanged.
    assert day.run(at("06:35"))["outcome"] == "unchanged"
    assert "calendar_write" not in day.run(at("06:36"))["seconds"]



def test_legacy_settle_after_minutes_config_still_loads_without_settling():
    settings = PlacementSettings.from_table({"enabled": True, "write": True, "settle_after_minutes": 1}, review_enabled=True)
    assert settings.settle_after_minutes == 1
    bank = item("bank")
    day = Day(bank, settle_after_minutes=settings.settle_after_minutes)
    day.pick(bank, at("06:30"))
    assert day.run(at("17:00"))["outcome"] == "not_settled"
    assert day.writer.calls == []
    day.settle(at("17:01"))
    assert day.run(at("17:02"))["outcome"] == "written"


def test_current_part_title_and_parent_survive_stored_plan_rehydration():
    send = item("send", minutes=15)
    edit = item("edit")
    edit = replace(edit, record=replace(edit.record, data={**edit.record.data, "parent": send.record.id, "order": 1,
                                                         "next_action": {"text": "Edit the brief", "done": False}}))
    day = Day(send, edit)
    day.pick(send, at("06:30"))
    day.settle(at("06:31"))
    assert day.run(at("06:32"))["outcome"] == "written"
    slots = sorted((entry for entry in day.writer.entries.values() if entry.role == "slot"), key=lambda entry: entry.slot.start)
    assert [entry.title for entry in slots] == ["\u25b8 Edit the brief (send)", "\u25b8 send"]
    assert slots[0].slot.end <= slots[1].slot.start
    assert day.run(at("06:35"))["outcome"] == "unchanged"
    assert "send" not in json.dumps(day.why_log.latest_placement(DAY)["plan"]).replace(send.record.id, "").replace(edit.record.id, "")


def test_stored_part_reorder_rewrites_next_ordinary_run_then_stays_unchanged():
    parent = item("parent", minutes=15)
    e1, e2 = item("e1"), item("e2")
    e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "parent": parent.record.id, "order": 1}))
    e2 = replace(e2, record=replace(e2.record, data={**e2.record.data, "parent": parent.record.id, "order": 2}))
    day = Day(parent, e1, e2)
    day.pick(parent, at("06:30"))
    day.settle(at("06:31"))

    def calendar_order():
        entries = sorted((entry for entry in day.writer.entries.values() if entry.role == "slot"),
                         key=lambda entry: entry.slot.start)
        return [entry.key.partition(":")[0] for entry in entries]

    assert day.run(at("06:32"))["outcome"] == "written"
    assert calendar_order() == [e1.record.id, e2.record.id, parent.record.id]
    e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "order": 2}))
    e2 = replace(e2, record=replace(e2.record, data={**e2.record.data, "order": 1}))
    day.items = [parent, e1, e2]
    day.writer.calls.clear()
    assert day.run(at("06:35"))["outcome"] == "written"
    assert calendar_order() == [e2.record.id, e1.record.id, parent.record.id]
    assert day.writer.calls
    day.writer.calls.clear()
    assert day.run(at("06:38"))["outcome"] == "unchanged"
    assert day.writer.calls == []
    assert "_order_after" not in json.dumps(day.why_log.latest_placement(DAY)["plan"])


@pytest.mark.parametrize("change", ["meeting", "new_first", "longer", "unfitting"])
def test_ordinary_run_reflows_future_chain_when_predecessor_changes(change):
    parent = item("parent", minutes=15)
    e1, e2 = item("e1"), item("e2")
    e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "parent": parent.record.id, "order": 1}))
    e2 = replace(e2, record=replace(e2.record, data={**e2.record.data, "parent": parent.record.id, "order": 2}))
    day = Day(parent, e1, e2)
    day.pick(parent, at("06:30"))
    day.settle(at("06:31"))
    assert day.run(at("06:32"))["outcome"] == "written"
    extra = []
    expected = [e1.record.id, e2.record.id, parent.record.id]
    if change == "meeting":
        day.events = [meeting("09:00", "09:20")]
    elif change == "new_first":
        e0 = item("e0")
        extra = [replace(e0, record=replace(e0.record, data={**e0.record.data, "parent": parent.record.id, "order": 1}))]
        e1 = replace(e1, record=replace(e1.record, data={**e1.record.data, "order": 2}))
        e2 = replace(e2, record=replace(e2.record, data={**e2.record.data, "order": 3}))
        expected.insert(0, e0.record.id)
    else:
        minutes = 60 if change == "longer" else 600
        e1 = replace(e1, record=replace(e1.record, data={**e1.record.data,
                     "timing": {"duration_minutes": minutes, "padded_minutes": minutes, "estimated_by": "nic"}}))
        if change == "unfitting":
            expected.remove(e1.record.id)
    day.items = [parent, e1, e2, *extra]
    day.writer.calls.clear()
    assert day.run(at("06:35"))["outcome"] == "written"
    entries = sorted((entry for entry in day.writer.entries.values() if entry.role == "slot"),
                     key=lambda entry: entry.slot.start)
    assert [entry.key.partition(":")[0] for entry in entries] == expected
    assert all(a.slot.end <= b.slot.start for a, b in zip(entries, entries[1:]))
    assert day.writer.calls
    day.writer.calls.clear()
    assert day.run(at("06:38"))["outcome"] == "unchanged"
    assert day.writer.calls == []
